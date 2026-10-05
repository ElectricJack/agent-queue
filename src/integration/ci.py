"""Strict trusted CI manifest, attestation, and durable evidence contracts."""

from __future__ import annotations
import time
import uuid
import re
from typing import Any
from sqlalchemy import insert, select, update
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_check_evidence,
    integration_branch_owners,
    integration_parent_verification_evidence,
    integration_parent_verifications,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    task_integration_checkpoints,
    tasks,
)
from src.integration.parent_engine import (
    parent_engine_guard,
)
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.outbox import enqueue_integration_event
import hashlib
import json
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Literal
from urllib.parse import quote
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
)
from src.git.github_contracts import (
    GitHubCredentialMode,
    credential_identity_from_client,
)








_OID = re.compile(r"^[0-9a-f]{40}$")

def required_checks(operation: dict[str, Any]) -> dict[str, Any]:
    policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
    return policy.parent.required_checks.model_dump(mode="json")


def binding_diagnosis(
    checkpoint: dict[str, Any],
    generation: int,
    head_sha: str,
    required: dict[str, Any],
    *,
    force_reason: str | None = None,
) -> dict[str, Any] | None:
    """Describe why no trusted verification binds this subject, or ``None``.

    ``verification_not_recorded`` is the plain wait (nothing has verified this
    generation and head); ``verified_other_head`` and
    ``verified_other_generation`` mean a verification exists for a different
    subject, so this one still has to be run; ``verification_record_missing`` is
    passed in by the caller for a checkpoint naming a verification that does not
    resolve to this operation. Every reason is a wait on somebody else's step --
    the aggregate's producer, its repair ladder, or the playbook rule -- and
    never a superseded subject, which is answered separately as
    ``stale_verification``.
    """
    verification_id = checkpoint["current_verification_id"]
    verified_generation = checkpoint["verified_generation"]
    verified_sha = checkpoint["verified_sha"]
    if force_reason is not None:
        reason = force_reason
    elif verification_id is None and verified_generation is None and verified_sha is None:
        reason = "verification_not_recorded"
    elif verified_sha is not None and verified_sha != head_sha:
        reason = "verified_other_head"
    elif verified_generation is not None and verified_generation != generation:
        reason = "verified_other_generation"
    elif verification_id is None:
        reason = "verification_not_recorded"
    else:
        return None
    return {
        "reason": reason,
        "generation": generation,
        "head_sha": head_sha,
        "verified_generation": verified_generation,
        "verified_sha": verified_sha,
        "verification_id": verification_id,
        "checkpoint_state": checkpoint["state"],
        "required_producer_id": required["producer_id"],
        "required_check_version": required["version"],
        "required_check_names": list(required["names"]),
        "next_owner": "parent_ci_producer",
        "next_action": (
            "Trusted integration check evidence is recorded by the daemon's "
            "parent CI producer and the parent-integration playbook, never by a "
            "worker's own test run. Read the parent blockers with "
            "`aq integration status <project_id>`; the durable Subject visit "
            "rechecks its CI evidence. When the producer records "
            "`task.integration_verified` for this generation and head, close "
            "again -- do not re-run the local suite."
        ),
    }







ATTESTATION_CHECK_NAME = "Agent Queue Integration Attestation"
TRUST_MANIFEST_PATH = ".github/agent-queue-integration.json"
_SHA_PATTERN = r"^[0-9a-f]{40}$"
_NUMERIC_PRODUCER = re.compile(r"[1-9][0-9]*")


def is_numeric_producer_id(producer_id: object) -> bool:
    """Whether a policy ``producer_id`` is the canonical numeric App id.

    The canonical producer is the producer App's id as a positive decimal
    string with no sign or leading zero (GitHub Actions is ``"15368"``), the
    value GitHub reports as ``check_run.app.id``. App credential mode requires
    it; a slug such as ``github-actions`` is legacy and keeps working only
    under existing-login credentials.
    """
    return isinstance(producer_id, str) and _NUMERIC_PRODUCER.fullmatch(producer_id) is not None


class AttestationError(ValueError):
    pass


class CIObservationDeferred(AttestationError):
    """Trusted observation is unfinished or could not validate the head.

    Legacy callers still see AttestationError; shared producers can retain
    the distinction without parsing diagnostic text.
    """

    def __init__(
        self, message: str, *, classification: Literal["none", "pending", "infra", "superseded"]
    ) -> None:
        super().__init__(message)
        self.classification = classification


SubjectTrustCause = Literal["missing", "too_large", "malformed", "identity_mismatch"]


class SubjectTrustError(AttestationError):
    """A subject tree's trust manifest is absent, oversized, malformed or names
    another identity, so the subject is refused (spec I4, I6)."""

    def __init__(
        self, cause: SubjectTrustCause, detail: str, *, fields: tuple[str, ...] = ()
    ) -> None:
        super().__init__(detail)
        self.cause = cause
        self.fields = fields


class RequiredChecksManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    names: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_nonempty_names(self) -> "RequiredChecksManifest":
        if any(not name.strip() for name in self.names) or len(set(self.names)) != len(self.names):
            raise ValueError("required check names must be unique and non-empty")
        return self


class IntegrationCITrust(BaseModel):
    """Repository and producer identity derived from frozen integration policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_repository_id: str = Field(min_length=1)
    repository_id: StrictInt = Field(gt=0)
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    producer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    required_checks: RequiredChecksManifest

    @model_validator(mode="after")
    def valid_producer_identity(self) -> "IntegrationCITrust":
        if self.producer_id.isdecimal() and int(self.producer_id) <= 0:
            raise ValueError("numeric CI producer identity must be positive")
        return self


def ci_trust_from_policy(
    *,
    canonical_repository_id: str,
    repository_id: int,
    full_name: str,
    policy: Any,
    boundary: Literal["parent", "root"],
) -> IntegrationCITrust:
    """Derive CI trust from one frozen project-policy boundary, without a repo manifest."""
    if isinstance(policy, BaseModel):
        policy = policy.model_dump(mode="json")
    if not isinstance(policy, Mapping):
        raise AttestationError("CI policy is malformed")
    selected = policy.get(boundary)
    if isinstance(selected, BaseModel):
        selected = selected.model_dump(mode="json")
    required = selected.get("required_checks") if isinstance(selected, Mapping) else None
    if isinstance(required, BaseModel):
        required = required.model_dump(mode="json")
    if not isinstance(required, Mapping):
        raise AttestationError(f"{boundary} CI policy is missing required checks")
    producer_id = required.get("producer_id")
    if not isinstance(producer_id, str):
        raise AttestationError("CI policy producer identity is malformed")
    try:
        return IntegrationCITrust(
            canonical_repository_id=canonical_repository_id,
            repository_id=repository_id,
            full_name=full_name,
            producer_id=producer_id,
            required_checks={
                "version": required.get("version"),
                "names": required.get("names"),
            },
        )
    except (TypeError, ValueError) as exc:
        raise AttestationError("CI policy producer or required checks are malformed") from exc


class IntegrationTrustManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: Literal["aq.integration-trust.v1"] = Field(alias="schema")
    canonical_repository_id: str = Field(min_length=1)
    repository_id: StrictInt = Field(gt=0)
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ci_producer_app_id: StrictInt = Field(gt=0)
    attestation_app_id: StrictInt = Field(gt=0)
    attestation_name: Literal["Agent Queue Integration Attestation"]
    required_checks: RequiredChecksManifest

    @model_validator(mode="after")
    def distinct_apps(self) -> "IntegrationTrustManifest":
        if self.ci_producer_app_id == self.attestation_app_id:
            raise ValueError("CI and attestation App identities must be distinct")
        return self


class AttestedCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    check_run_id: StrictInt = Field(gt=0)
    check_suite_id: StrictInt = Field(gt=0)
    producer_app_id: StrictInt = Field(gt=0)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    conclusion: Literal["success"]


class CIReceiptCheck(AttestedCheck):
    """A required check with both configured and live GitHub producer identity."""

    producer_id: str = Field(min_length=1)


class AttestedWorkflowRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_run_id: StrictInt = Field(gt=0)
    run_attempt: StrictInt = Field(gt=0)
    check_suite_id: StrictInt = Field(gt=0)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    conclusion: Literal["success"]


class AttestationPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: Literal["aq.integration-attestation.v1"] = Field(alias="schema")
    canonical_repository_id: str = Field(min_length=1)
    repository_id: StrictInt = Field(gt=0)
    ci_producer_app_id: StrictInt = Field(gt=0)
    attestation_app_id: StrictInt = Field(gt=0)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    required_check_set_version: str = Field(min_length=1)
    checks: tuple[AttestedCheck, ...] = Field(min_length=1)
    workflow_runs: tuple[AttestedWorkflowRun, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_attempts(self) -> "AttestationPayload":
        names = [check.name for check in self.checks]
        check_ids = [check.check_run_id for check in self.checks]
        suites = [workflow.check_suite_id for workflow in self.workflow_runs]
        if len(set(names)) != len(names) or len(set(check_ids)) != len(check_ids):
            raise ValueError("attested checks contain duplicates")
        if len(set(suites)) != len(suites):
            raise ValueError("workflow attempts contain duplicate suites")
        workflow_by_suite = {workflow.check_suite_id: workflow for workflow in self.workflow_runs}
        if set(workflow_by_suite) != {check.check_suite_id for check in self.checks}:
            raise ValueError("workflow attempt coverage does not match check suites")
        for check in self.checks:
            workflow = workflow_by_suite[check.check_suite_id]
            if check.head_sha != self.head_sha or workflow.head_sha != self.head_sha:
                raise ValueError("attestation head identity is incoherent")
        return self

    @classmethod
    def from_canonical_bytes(cls, value: bytes) -> "AttestationPayload":
        try:
            decoded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, AttestationError) as exc:
            raise AttestationError("attestation JSON is invalid") from exc
        payload = cls.model_validate(decoded)
        if payload.canonical_bytes() != value:
            raise AttestationError("attestation bytes are noncanonical")
        return payload

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    @property
    def external_id(self) -> str:
        return "aq-attestation-v1:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


class CIReceiptPayload(BaseModel):
    """Canonical live-GitHub evidence retained by the daemon, not published as a check."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_: Literal["aq.integration-ci-receipt.v1"] = Field(alias="schema")
    canonical_repository_id: str = Field(min_length=1)
    repository_id: StrictInt = Field(gt=0)
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    producer_id: str = Field(min_length=1)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    required_check_set_version: str = Field(min_length=1)
    checks: tuple[CIReceiptCheck, ...] = Field(min_length=1)
    workflow_runs: tuple[AttestedWorkflowRun, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_attempts(self) -> "CIReceiptPayload":
        _validate_check_workflow_coverage(self.checks, self.workflow_runs, self.head_sha)
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    @property
    def external_id(self) -> str:
        return "aq-ci-receipt-v1:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class SelectedAttestation:
    record_id: int
    payload: AttestationPayload


def select_trusted_attestation(
    records: list[dict[str, Any]],
    trust: IntegrationTrustManifest,
    *,
    expected_head_sha: str,
) -> SelectedAttestation:
    trusted: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        app = record.get("app")
        if record.get("name") != trust.attestation_name:
            continue
        app_id = _strict_int(app.get("id")) if isinstance(app, dict) else None
        if app_id is None or app_id <= 0:
            raise AttestationError("exact-name attestation App identity is malformed")
        if app_id != trust.attestation_app_id:
            continue
        record_id = _strict_int(record.get("id"))
        if record_id is None or record_id <= 0:
            raise AttestationError("trusted attestation ordering identity is malformed")
        trusted.append((record_id, record))
    if not trusted:
        raise AttestationError("trusted attestation is missing")
    selected_id, newest = max(trusted, key=lambda item: item[0])
    try:
        if (
            newest.get("status") != "completed"
            or newest.get("conclusion") != "success"
            or newest.get("head_sha") != expected_head_sha
        ):
            raise AttestationError("newest trusted attestation is not successful")
        output = newest.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("text"), str):
            raise AttestationError("newest trusted attestation payload is missing")
        payload = AttestationPayload.from_canonical_bytes(output["text"].encode("utf-8"))
        if newest.get("external_id") != payload.external_id:
            raise AttestationError("newest trusted attestation digest does not match")
        if (
            payload.canonical_repository_id != trust.canonical_repository_id
            or payload.repository_id != trust.repository_id
            or payload.ci_producer_app_id != trust.ci_producer_app_id
            or payload.attestation_app_id != trust.attestation_app_id
            or payload.head_sha != expected_head_sha
            or payload.required_check_set_version != trust.required_checks.version
            or tuple(check.name for check in payload.checks) != trust.required_checks.names
            or any(check.producer_app_id != trust.ci_producer_app_id for check in payload.checks)
        ):
            raise AttestationError("newest trusted attestation identity does not match")
        return SelectedAttestation(record_id=selected_id, payload=payload)
    except AttestationError:
        raise
    except Exception as exc:
        raise AttestationError("newest trusted attestation is invalid") from exc


@dataclass(frozen=True)
class TrustedCIObservation:
    payload: AttestationPayload | CIReceiptPayload
    workflow_ids: dict[int, int]


@dataclass(frozen=True)
class FailedCIObservation:
    checks: tuple[dict[str, Any], ...]
    workflow_runs: tuple[dict[str, Any], ...]
    workflow_ids: dict[int, int]
    conclusion: Literal["failure", "cancelled", "inconclusive"]


class ParentCISubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=1)
    parent_task_id: str = Field(min_length=1)
    generation: StrictInt = Field(ge=0)
    head_sha: str = Field(pattern=_SHA_PATTERN)


class CandidateCISubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    revision: StrictInt = Field(ge=0)
    candidate_sha: str = Field(pattern=_SHA_PATTERN)


class IntegrationCIEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    operation_id: str
    batch_id: str | None = None
    candidate_revision: int | None = None
    parent_task_id: str | None = None
    parent_generation: int | None = None
    parent_head_sha: str | None = None
    producer_id: str
    workflow_id: str
    run_id: str
    attempt: int = Field(ge=0)
    required_check_version: str
    checks: dict[str, Literal["success", "failure", "cancelled", "skipped", "neutral", "missing"]]
    conclusion: Literal["success", "failure", "cancelled", "inconclusive"]
    classification: Literal["conclusive", "full_suite_fallback"]
    observed_at: float

    @model_validator(mode="after")
    def exact_subject(self) -> "IntegrationCIEvidence":
        parent = (
            self.batch_id is None
            and self.candidate_revision is None
            and self.parent_task_id is not None
            and self.parent_generation is not None
            and self.parent_head_sha is not None
        )
        candidate = (
            self.batch_id is not None
            and self.candidate_revision is not None
            and self.parent_task_id is None
            and self.parent_generation is None
            and self.parent_head_sha is None
        )
        if parent == candidate:
            raise ValueError("CI evidence must bind exactly one typed subject")
        if not self.checks:
            raise ValueError("CI evidence checks must be non-empty")
        return self


class TrustedFixtureObserver:
    """Explicit test-only observation seam; production uses authenticated GitHub reads."""

    def __init__(self, observation: TrustedCIObservation | FailedCIObservation):
        self.observation = observation

    async def observe(
        self, trust: IntegrationTrustManifest | IntegrationCITrust, head_sha: str
    ) -> TrustedCIObservation | FailedCIObservation:
        if isinstance(self.observation, TrustedCIObservation):
            _require_payload_matches_trust(self.observation.payload, trust, head_sha)
        return self.observation


class IntegrationCIEvidenceAdapter:
    """Append-only normalized evidence writer that retains the caller transaction."""

    async def append_on(self, conn: Any, evidence: IntegrationCIEvidence) -> str:
        duplicate = (
            await conn.execute(
                select(integration_check_evidence).where(
                    # Exact-commit cache rows (checks.py) share run identities.
                    integration_check_evidence.c.sha.is_(None),
                    integration_check_evidence.c.producer_id == evidence.producer_id,
                    integration_check_evidence.c.run_id == evidence.run_id,
                    integration_check_evidence.c.attempt == evidence.attempt,
                    integration_check_evidence.c.required_check_version
                    == evidence.required_check_version,
                )
            )
        ).mappings().one_or_none()
        values = evidence.model_dump()
        if duplicate is not None:
            if all(
                duplicate[key] == value
                for key, value in values.items()
                if key not in {"id", "observed_at"}
            ):
                return duplicate["id"]
            raise AttestationError("CI attempt was already bound to another subject")
        await conn.execute(insert(integration_check_evidence).values(**values))
        return evidence.id


class CIService:
    """Observe typed integration subjects and durably append normalized trusted evidence."""

    def __init__(
        self,
        db: Any,
        trust: IntegrationTrustManifest | IntegrationCITrust,
        observer: Any,
        *,
        clock=time.time,
    ):
        if not isinstance(observer, (AuthenticatedGitHubObserver, TrustedFixtureObserver)):
            raise TypeError("CIService requires an authenticated or explicit fixture observer")
        if isinstance(observer, AuthenticatedGitHubObserver):
            client = observer.client
            identity = credential_identity_from_client(client)
            if (
                client.repository.repository_id != trust.repository_id
                or client.repository.full_name != trust.full_name
                or client.repository.forge_host != "github.com"
                or (
                    isinstance(trust, IntegrationTrustManifest)
                    and (
                        identity.mode is not GitHubCredentialMode.APP
                        or identity.app_id != trust.attestation_app_id
                    )
                )
                or (
                    isinstance(trust, IntegrationCITrust)
                    and identity.mode is not GitHubCredentialMode.EXISTING_LOGIN
                )
            ):
                raise ValueError("authenticated provider identity does not match CI trust")
        self.db = db
        self.trust = trust
        self.observer = observer
        self.clock = clock
        self.adapter = IntegrationCIEvidenceAdapter()

    async def observe_parent(self, subject: ParentCISubject) -> dict[str, Any]:
        if not isinstance(subject, ParentCISubject):
            raise TypeError("observe_parent requires ParentCISubject")
        async with self.db.immediate() as conn:
            declared_trust = await self._lock_parent_subject_on(conn, subject)
        if declared_trust is None:
            return {"outcome": "stale_subject", "evidence_ids": []}
        try:
            observation = await self.observer.observe(declared_trust, subject.head_sha)
        except AttestationError:
            outcome = (
                "full_suite_required"
                if declared_trust.required_checks != self.trust.required_checks
                else "not_green"
            )
            return {"outcome": outcome, "evidence_ids": []}
        async with self.db.immediate() as conn:
            rechecked_trust = await self._lock_parent_subject_on(conn, subject)
            if rechecked_trust != declared_trust:
                return {"outcome": "stale_subject", "evidence_ids": []}
            evidence_ids = await self._append_observation_on(
                conn, subject, observation, trust=declared_trust
            )
            return {
                "outcome": "green" if isinstance(observation, TrustedCIObservation) else "red",
                "evidence_ids": evidence_ids,
            }

    async def observe_candidate(self, subject: CandidateCISubject) -> dict[str, Any]:
        if not isinstance(subject, CandidateCISubject):
            raise TypeError("observe_candidate requires CandidateCISubject")
        async with self.db.immediate() as conn:
            current = await self._lock_candidate_subject_on(conn, subject)
        if not current:
            return {"outcome": "stale_subject", "evidence_ids": []}
        try:
            observation = await self.observer.observe(self.trust, subject.candidate_sha)
        except AttestationError:
            return {"outcome": "not_green", "evidence_ids": []}
        async with self.db.immediate() as conn:
            if not await self._lock_candidate_subject_on(conn, subject):
                return {"outcome": "stale_subject", "evidence_ids": []}
            evidence_ids = await self._append_observation_on(
                conn, subject, observation, trust=self.trust
            )
            aggregate_evidence_id = None
            if isinstance(observation, TrustedCIObservation):
                aggregate_evidence_id = await self._project_candidate_green_on(
                    conn, subject, observation
                )
            return {
                "outcome": "green" if isinstance(observation, TrustedCIObservation) else "red",
                "evidence_ids": evidence_ids,
                **(
                    {"aggregate_evidence_id": aggregate_evidence_id}
                    if aggregate_evidence_id is not None
                    else {}
                ),
            }

    async def _project_candidate_green_on(
        self,
        conn: Any,
        subject: CandidateCISubject,
        observation: TrustedCIObservation,
    ) -> str:
        """Persist the one aggregate success identity consumed by promotion."""
        payload = observation.payload
        identity = {
            "subject": subject.model_dump(),
            "attestation": payload.external_id,
            "workflows": sorted(observation.workflow_ids.items()),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        aggregate_id = f"ci-aggregate-{digest}"
        evidence = IntegrationCIEvidence(
            id=aggregate_id,
            operation_id=subject.operation_id,
            batch_id=subject.batch_id,
            candidate_revision=subject.revision,
            producer_id=_payload_producer_id(payload),
            workflow_id="aggregate:" + digest,
            run_id=payload.external_id,
            attempt=0,
            required_check_version=payload.required_check_set_version,
            checks={check.name: "success" for check in payload.checks},
            conclusion="success",
            classification="conclusive",
            observed_at=self.clock(),
        )
        aggregate_id = await self.adapter.append_on(conn, evidence)
        revision_changed = await conn.execute(
            update(integration_candidate_revisions)
            .where(
                integration_candidate_revisions.c.batch_id == subject.batch_id,
                integration_candidate_revisions.c.revision == subject.revision,
                integration_candidate_revisions.c.head_sha == subject.candidate_sha,
                integration_candidate_revisions.c.state.in_(("built", "testing", "green")),
            )
            .values(state="green", ci_evidence_id=aggregate_id, updated_at=self.clock())
        )
        batch_changed = await conn.execute(
            update(integration_batches)
            .where(
                integration_batches.c.id == subject.batch_id,
                integration_batches.c.current_revision == subject.revision,
                integration_batches.c.lifecycle.in_(("testing", "repairing")),
            )
            .values(
                lifecycle="testing",
                tested_candidate_sha=subject.candidate_sha,
                ci_evidence_id=aggregate_id,
                updated_at=self.clock(),
            )
        )
        if revision_changed.rowcount != 1 or batch_changed.rowcount != 1:
            raise AttestationError("candidate changed before aggregate green projection")
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == subject.operation_id,
                integration_repair_stages.c.state == "active",
                integration_repair_stages.c.ordinal == select(
                    integration_repair_operations.c.active_stage
                ).where(integration_repair_operations.c.id == subject.operation_id).scalar_subquery(),
            )
            .values(
                state="awaiting_completion",
                current_subject={
                    "kind": "batch", "revision": subject.revision,
                    "candidate_sha": subject.candidate_sha,
                },
                success_subject={
                    "kind": "batch", "revision": subject.revision,
                    "candidate_sha": subject.candidate_sha,
                },
                success_evidence_id=aggregate_id,
            )
        )
        return aggregate_id

    async def _lock_parent_subject_on(
        self, conn: Any, subject: ParentCISubject
    ) -> IntegrationTrustManifest | IntegrationCITrust | None:
        parent = (
            await conn.execute(select(tasks).where(tasks.c.id == subject.parent_task_id))
        ).mappings().one_or_none()
        project = None
        if parent is not None:
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == parent["project_id"])
                )
            ).mappings().one_or_none()
        if not self._enabled_project_repository(project):
            return None
        await self.db.lock_hierarchy_project(conn, project["id"])
        project = (
            await conn.execute(
                select(projects)
                .where(projects.c.id == project["id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        parent = (
            await conn.execute(
                select(tasks)
                .where(tasks.c.id == subject.parent_task_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        if (
            not self._enabled_project_repository(project)
            or parent is None
            or parent["project_id"] != project["id"]
        ):
            return None
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == subject.parent_task_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        operation = None
        if checkpoint is not None and checkpoint["episode_id"] is not None:
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.parent_task_id
                        == subject.parent_task_id,
                        integration_repair_operations.c.episode_id
                        == checkpoint["episode_id"],
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
        declared_trust = self._operation_trust(operation, "parent")
        if (
            operation is None
            or checkpoint is None
            or parent["repo_id"] != self.trust.canonical_repository_id
            or checkpoint["repository_id"] != self.trust.canonical_repository_id
            or checkpoint["state"] != "verifying"
            or operation["target_kind"] != "parent"
            or operation["id"] != subject.operation_id
            or operation["parent_task_id"] != subject.parent_task_id
            or operation["episode_id"] != checkpoint["episode_id"]
            or operation["state"] not in {"active", "escalated"}
            or declared_trust is None
            or checkpoint["generation"] != subject.generation
            or checkpoint["checkpoint_sha"] != subject.head_sha
        ):
            return None
        return declared_trust

    async def _lock_candidate_subject_on(
        self, conn: Any, subject: CandidateCISubject
    ) -> bool:
        initial_batch = (
            await conn.execute(
                select(integration_batches).where(
                    integration_batches.c.id == subject.batch_id
                )
            )
        ).mappings().one_or_none()
        project = None
        if initial_batch is not None:
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == initial_batch["project_id"])
                )
            ).mappings().one_or_none()
        if not self._enabled_project_repository(project):
            return False
        await self.db.lock_hierarchy_project(conn, project["id"])
        project = (
            await conn.execute(
                select(projects)
                .where(projects.c.id == project["id"])
                .with_for_update()
            )
        ).mappings().one_or_none()
        if not self._enabled_project_repository(project):
            return False
        batch = (
            await conn.execute(
                select(integration_batches)
                .where(integration_batches.c.id == subject.batch_id)
                .with_for_update()
            )
        ).mappings().one_or_none()
        candidate = (
            await conn.execute(
                select(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == subject.batch_id,
                    integration_candidate_revisions.c.revision == subject.revision,
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        operation = None
        if batch is not None:
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(
                        integration_repair_operations.c.batch_id == batch["id"],
                        integration_repair_operations.c.episode_id == batch["id"],
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
        return bool(
            operation is not None
            and batch is not None
            and candidate is not None
            and batch["project_id"] == project["id"]
            and batch["repository_id"] == self.trust.canonical_repository_id
            and batch["current_revision"] == subject.revision
            and batch["lifecycle"] in {"testing", "repairing"}
            and operation["target_kind"] == "batch"
            and operation["id"] == subject.operation_id
            and operation["batch_id"] == subject.batch_id
            and operation["episode_id"] == batch["id"]
            and operation["state"] in {"active", "escalated"}
            and self._operation_trust(operation, "root") == self.trust
            and candidate["head_sha"] == subject.candidate_sha
            and candidate["state"] in {"built", "testing", "green"}
        )

    def _enabled_project_repository(self, project: Any | None) -> bool:
        return bool(
            project is not None
            and project["status"] == "ACTIVE"
            and project["hierarchical_integration_mode"] in {"hierarchy", "train"}
            and project["integration_repository_id"]
            == self.trust.canonical_repository_id
        )

    async def _append_observation_on(
        self,
        conn: Any,
        subject: ParentCISubject | CandidateCISubject,
        observation: TrustedCIObservation | FailedCIObservation,
        *,
        trust: IntegrationTrustManifest | IntegrationCITrust,
    ) -> list[str]:
        expected_head = (
            subject.head_sha if isinstance(subject, ParentCISubject) else subject.candidate_sha
        )
        if isinstance(observation, TrustedCIObservation):
            payload = observation.payload
            _require_payload_matches_trust(payload, trust, expected_head)
            producer_id = _payload_producer_id(payload)
            version = payload.required_check_set_version
            check_rows = [check.model_dump() for check in payload.checks]
            workflow_rows = [workflow.model_dump() for workflow in payload.workflow_runs]
            overall_conclusion = "success"
        else:
            producer_id = _trust_producer_id(trust)
            version = trust.required_checks.version
            check_rows = list(observation.checks)
            workflow_rows = list(observation.workflow_runs)
            overall_conclusion = observation.conclusion
        evidence_ids: list[str] = []
        workflows = {workflow["check_suite_id"]: workflow for workflow in workflow_rows}
        for suite_id, workflow in workflows.items():
            checks = {
                check["name"]: check["conclusion"]
                for check in check_rows
                if check["check_suite_id"] == suite_id
            }
            workflow_id = observation.workflow_ids.get(suite_id)
            if workflow_id is None:
                raise AttestationError("trusted observation omitted workflow identity")
            identity = {
                "subject": subject.model_dump(),
                "producer": producer_id,
                "workflow": workflow_id,
                "run": workflow["workflow_run_id"],
                "attempt": workflow["run_attempt"],
                "version": version,
            }
            evidence_id = "ci-" + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            subject_values = (
                {
                    "batch_id": None,
                    "candidate_revision": None,
                    "parent_task_id": subject.parent_task_id,
                    "parent_generation": subject.generation,
                    "parent_head_sha": subject.head_sha,
                }
                if isinstance(subject, ParentCISubject)
                else {
                    "batch_id": subject.batch_id,
                    "candidate_revision": subject.revision,
                    "parent_task_id": None,
                    "parent_generation": None,
                    "parent_head_sha": None,
                }
            )
            evidence = IntegrationCIEvidence(
                id=evidence_id,
                operation_id=subject.operation_id,
                producer_id=str(producer_id),
                workflow_id=str(workflow_id),
                run_id=str(workflow["workflow_run_id"]),
                attempt=workflow["run_attempt"],
                required_check_version=version,
                checks=checks,
                conclusion=overall_conclusion,
                classification="conclusive",
                observed_at=self.clock(),
                **subject_values,
            )
            evidence_ids.append(await self.adapter.append_on(conn, evidence))
        return sorted(evidence_ids)

    def _operation_trust(
        self, operation: Any | None, boundary: str
    ) -> IntegrationTrustManifest | IntegrationCITrust | None:
        if operation is None:
            return None
        snapshot = operation["policy_snapshot"]
        configured = (
            snapshot.get(boundary, {}).get("required_checks", {})
            if isinstance(snapshot, dict)
            else {}
        )
        if (
            not isinstance(configured, dict)
            or configured.get("version") != operation["required_check_version"]
            or configured.get("producer_id") != _trust_producer_id(self.trust)
        ):
            return None
        try:
            required = RequiredChecksManifest(
                version=configured["version"], names=tuple(configured["names"])
            )
            derived = self.trust.model_copy(update={"required_checks": required})
        except (KeyError, TypeError, ValueError):
            return None
        if boundary == "root" and derived != self.trust:
            return None
        return derived


class AuthenticatedGitHubObserver:
    """Build canonical evidence exclusively from authenticated GitHub API reads."""

    def __init__(self, client: Any, *, expected_event: str | None = None):
        self.client = client
        self.expected_event = expected_event

    async def observe(
        self, trust: IntegrationTrustManifest | IntegrationCITrust, head_sha: str
    ) -> TrustedCIObservation | FailedCIObservation:
        if not isinstance(head_sha, str) or re.fullmatch(_SHA_PATTERN, head_sha) is None:
            raise AttestationError("invalid CI head")
        owner, repository = trust.full_name.split("/", 1)
        workflow_records = await self.client.paged_items(
            f"/repos/{owner}/{repository}/actions/runs?head_sha={head_sha}&per_page=100",
            key="workflow_runs",
        )
        allowed_suites = (
            {
                _strict_int(record.get("check_suite_id"))
                for record in workflow_records
                if record.get("event") == self.expected_event
            }
            if self.expected_event is not None
            else None
        )
        selected: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in trust.required_checks.names:
            path = (
                f"/repos/{owner}/{repository}/commits/{head_sha}/check-runs"
                f"?check_name={quote(name, safe='')}&filter=all&per_page=100"
            )
            records = await self.client.paged_items(path, key="check_runs")
            candidates: list[tuple[int, dict[str, Any]]] = []
            for record in records:
                app = record.get("app")
                if record.get("name") != name:
                    continue
                app_id = _strict_int(app.get("id")) if isinstance(app, dict) else None
                if app_id is None or app_id <= 0:
                    raise AttestationError(
                        f"required check App identity is malformed: {name}"
                    )
                if not _producer_matches(app, trust):
                    continue
                record_id = _strict_int(record.get("id"))
                if record_id is None or record_id <= 0:
                    raise AttestationError(
                        f"required check ordering identity is malformed: {name}"
                    )
                suite = record.get("check_suite")
                suite_id = _strict_int(suite.get("id")) if isinstance(suite, dict) else None
                if allowed_suites is not None and suite_id not in allowed_suites:
                    continue
                candidates.append((record_id, record))
            if not candidates:
                missing.append(name)
                continue
            _, newest = max(candidates, key=lambda item: item[0])
            selected_app = newest.get("app")
            selected_app_id = (
                _strict_int(selected_app.get("id"))
                if isinstance(selected_app, dict)
                else None
            )
            if selected_app_id is None or selected_app_id <= 0:
                raise AttestationError(f"required check App identity is malformed: {name}")
            suite = newest.get("check_suite")
            suite_id = _strict_int(suite.get("id")) if isinstance(suite, dict) else None
            conclusion = newest.get("conclusion")
            if newest.get("head_sha") == head_sha:
                if newest.get("status") != "completed":
                    raise CIObservationDeferred(
                        f"required check is not conclusive: {name}", classification="pending"
                    )
                if conclusion in {"timed_out", "action_required", "startup_failure", "stale"}:
                    raise CIObservationDeferred(
                        f"required check could not validate: {name} ({conclusion})",
                        classification="infra",
                    )
            if (
                newest.get("status") != "completed"
                or conclusion not in {"success", "failure", "cancelled", "skipped", "neutral"}
                or newest.get("head_sha") != head_sha
                or suite_id is None
                or suite_id <= 0
            ):
                raise AttestationError(f"required check is not conclusive: {name}")
            selected.append(
                {
                    "name": name,
                    "check_run_id": newest["id"],
                    "check_suite_id": suite_id,
                    "producer_app_id": selected_app_id,
                    "head_sha": head_sha,
                    "conclusion": conclusion,
                    **(
                        {"producer_id": trust.producer_id}
                        if isinstance(trust, IntegrationCITrust)
                        else {}
                    ),
                }
            )

        missing_suite_id: int | None = None
        if missing:
            # An empty check list immediately after a push is not a CI failure.
            # Only completed push workflows for this exact head can establish
            # that the snapshot's required name was never produced.
            push_runs = [record for record in workflow_records if record.get("event") == "push"]
            if not push_runs or any(record.get("status") != "completed" for record in push_runs):
                raise CIObservationDeferred(
                    f"required check is pending: {', '.join(missing)}",
                    classification="pending" if workflow_records or selected else "none",
                )
            for record in push_runs:
                suite_id = _strict_int(record.get("check_suite_id"))
                if (
                    record.get("head_sha") != head_sha
                    or suite_id is None
                    or suite_id <= 0
                    or _strict_int(record.get("id")) is None
                    or _strict_int(record.get("workflow_id")) is None
                    or _strict_int(record.get("run_attempt")) is None
                    or record.get("conclusion")
                    not in {"success", "failure", "cancelled", "skipped", "neutral"}
                ):
                    raise AttestationError("completed push workflow identity is malformed")
                if isinstance(trust, IntegrationCITrust):
                    _require_workflow_repository(record, trust)
            # Anchor the absence to one real completed suite. Prefer a suite
            # already carrying a selected required check, so each evidence row
            # still describes a single workflow attempt.
            selected_suites = {check["check_suite_id"] for check in selected}
            missing_suite_id = min(
                (
                    _strict_int(record["check_suite_id"])
                    for record in push_runs
                    if _strict_int(record["check_suite_id"]) in selected_suites
                ),
                default=min(_strict_int(record["check_suite_id"]) for record in push_runs),
            )

        workflow_rows: list[dict[str, Any]] = []
        workflow_ids: dict[int, int] = {}
        suite_ids = dict.fromkeys(
            [check["check_suite_id"] for check in selected]
            + ([missing_suite_id] if missing_suite_id is not None else [])
        )
        for suite_id in suite_ids:
            matches = [
                record
                for record in workflow_records
                if _strict_int(record.get("check_suite_id")) == suite_id
            ]
            if not matches:
                raise AttestationError("workflow attempt identity is missing or ambiguous")
            ordered: list[tuple[int, int, dict[str, Any]]] = []
            for candidate in matches:
                candidate_id = _strict_int(candidate.get("id"))
                candidate_attempt = _strict_int(candidate.get("run_attempt"))
                if (
                    candidate_id is None
                    or candidate_id <= 0
                    or candidate_attempt is None
                    or candidate_attempt <= 0
                ):
                    raise AttestationError("workflow attempt ordering identity is malformed")
                ordered.append((candidate_attempt, candidate_id, candidate))
            _, _, record = max(ordered, key=lambda item: (item[0], item[1]))
            workflow_run_id = _strict_int(record.get("id"))
            workflow_id = _strict_int(record.get("workflow_id"))
            run_attempt = _strict_int(record.get("run_attempt"))
            if record.get("head_sha") == head_sha:
                if record.get("status") is not None and record["status"] != "completed":
                    raise CIObservationDeferred(
                        "workflow attempt is not conclusive", classification="pending"
                    )
                if record.get("conclusion") in {
                    "timed_out", "action_required", "startup_failure", "stale"
                }:
                    raise CIObservationDeferred(
                        "workflow attempt could not validate", classification="infra"
                    )
            if (
                workflow_run_id is None
                or workflow_run_id <= 0
                or workflow_id is None
                or workflow_id <= 0
                or run_attempt is None
                or run_attempt <= 0
                or record.get("head_sha") != head_sha
                or record.get("conclusion")
                not in {"success", "failure", "cancelled", "skipped", "neutral"}
            ):
                raise AttestationError("workflow attempt is not conclusive")
            if isinstance(trust, IntegrationCITrust):
                _require_workflow_repository(record, trust)
                jobs = await self.client.paged_items(
                    f"/repos/{owner}/{repository}/actions/runs/{workflow_run_id}"
                    f"/attempts/{run_attempt}/jobs?per_page=100",
                    key="jobs",
                )
                _require_latest_attempt_jobs(
                    jobs,
                    checks=[
                        check
                        for check in selected
                        if check["check_suite_id"] == suite_id
                    ],
                    workflow_run_id=workflow_run_id,
                    run_attempt=run_attempt,
                    head_sha=head_sha,
                    full_name=trust.full_name,
                )
            workflow_rows.append(
                {
                    "workflow_run_id": workflow_run_id,
                    "run_attempt": run_attempt,
                    "check_suite_id": suite_id,
                    "head_sha": head_sha,
                    "conclusion": record["conclusion"],
                }
            )
            workflow_ids[suite_id] = workflow_id
        if missing_suite_id is not None:
            selected.extend(
                {"name": name, "check_suite_id": missing_suite_id, "conclusion": "missing"}
                for name in missing
            )
        if any(check["conclusion"] != "success" for check in selected) or any(
            workflow["conclusion"] != "success" for workflow in workflow_rows
        ):
            conclusions = {check["conclusion"] for check in selected} | {
                workflow["conclusion"] for workflow in workflow_rows
            }
            # Cancelled jobs do not make a run inconclusive when some required
            # check or workflow genuinely failed: a real failure dominates, so
            # the run is RED/repairable rather than infra. Only a run whose
            # non-success jobs are all cancelled stays inconclusive.
            overall = (
                "cancelled" if "cancelled" in conclusions and "failure" not in conclusions
                else "failure"
            )
            return FailedCIObservation(
                checks=tuple(selected),
                workflow_runs=tuple(workflow_rows),
                workflow_ids=workflow_ids,
                conclusion=overall,
            )
        common = {
            "canonical_repository_id": trust.canonical_repository_id,
            "repository_id": trust.repository_id,
            "head_sha": head_sha,
            "required_check_set_version": trust.required_checks.version,
            "workflow_runs": tuple(
                AttestedWorkflowRun.model_validate(workflow) for workflow in workflow_rows
            ),
        }
        if isinstance(trust, IntegrationTrustManifest):
            payload: AttestationPayload | CIReceiptPayload = AttestationPayload(
                schema="aq.integration-attestation.v1",
                ci_producer_app_id=trust.ci_producer_app_id,
                attestation_app_id=trust.attestation_app_id,
                checks=tuple(AttestedCheck.model_validate(check) for check in selected),
                **common,
            )
        else:
            payload = CIReceiptPayload(
                schema="aq.integration-ci-receipt.v1",
                full_name=trust.full_name,
                producer_id=trust.producer_id,
                checks=tuple(CIReceiptCheck.model_validate(check) for check in selected),
                **common,
            )
        return TrustedCIObservation(payload=payload, workflow_ids=workflow_ids)

    async def publish(
        self, trust: IntegrationTrustManifest, payload: AttestationPayload
    ) -> int:
        """Publish one completed canonical attestation through the configured App."""
        _require_payload_matches_trust(payload, trust, payload.head_sha)
        owner, repository = trust.full_name.split("/", 1)
        existing = await self.client.paged_items(
            f"/repos/{owner}/{repository}/commits/{payload.head_sha}/check-runs"
            f"?check_name={quote(trust.attestation_name, safe='')}&filter=all&per_page=100",
            key="check_runs",
        )
        trusted_existing: list[tuple[int, dict[str, Any]]] = []
        for record in existing:
            app = record.get("app")
            if record.get("name") != trust.attestation_name:
                continue
            app_id = _strict_int(app.get("id")) if isinstance(app, dict) else None
            if app_id is None or app_id <= 0:
                raise AttestationError("trusted attestation App identity is malformed")
            if app_id != trust.attestation_app_id:
                continue
            record_id = _strict_int(record.get("id"))
            if record_id is None or record_id <= 0:
                raise AttestationError(
                    "trusted attestation ordering identity is malformed"
                )
            trusted_existing.append((record_id, record))
        if trusted_existing:
            _, newest = max(trusted_existing, key=lambda item: item[0])
            output = newest.get("output")
            if (
                newest.get("status") == "completed"
                and newest.get("conclusion") == "success"
                and newest.get("head_sha") == payload.head_sha
                and newest.get("external_id") == payload.external_id
                and isinstance(output, dict)
                and output.get("text") == payload.canonical_bytes().decode("ascii")
            ):
                return newest["id"]
        result = await self.client.request_json(
            "POST",
            f"/repos/{owner}/{repository}/check-runs",
            json_body={
                "name": trust.attestation_name,
                "head_sha": payload.head_sha,
                "status": "completed",
                "conclusion": "success",
                "external_id": payload.external_id,
                "output": {
                    "title": trust.attestation_name,
                    "summary": "Authenticated Agent Queue integration evidence",
                    "text": payload.canonical_bytes().decode("ascii"),
                },
            },
            expected_statuses={201},
        )
        record_id = _strict_int(result.get("id"))
        if record_id is None or record_id <= 0:
            raise AttestationError("published attestation identity is malformed")
        return record_id


def _require_payload_matches_trust(
    payload: AttestationPayload | CIReceiptPayload,
    trust: IntegrationTrustManifest | IntegrationCITrust,
    expected_head_sha: str,
) -> None:
    if isinstance(trust, IntegrationCITrust):
        if (
            not isinstance(payload, CIReceiptPayload)
            or payload.canonical_repository_id != trust.canonical_repository_id
            or payload.repository_id != trust.repository_id
            or payload.full_name != trust.full_name
            or payload.producer_id != trust.producer_id
            or payload.head_sha != expected_head_sha
            or payload.required_check_set_version != trust.required_checks.version
            or tuple(check.name for check in payload.checks) != trust.required_checks.names
            or any(check.producer_id != trust.producer_id for check in payload.checks)
            or any(
                not _check_producer_app_id_matches(check.producer_app_id, trust.producer_id)
                for check in payload.checks
            )
        ):
            raise AttestationError("CI receipt identity does not match policy trust")
        return
    if not isinstance(payload, AttestationPayload):
        raise AttestationError("attestation identity does not match trust manifest")
    if (
        payload.canonical_repository_id != trust.canonical_repository_id
        or payload.repository_id != trust.repository_id
        or payload.ci_producer_app_id != trust.ci_producer_app_id
        or payload.attestation_app_id != trust.attestation_app_id
        or payload.head_sha != expected_head_sha
        or payload.required_check_set_version != trust.required_checks.version
        or tuple(check.name for check in payload.checks) != trust.required_checks.names
        or any(check.producer_app_id != trust.ci_producer_app_id for check in payload.checks)
    ):
        raise AttestationError("attestation identity does not match trust manifest")


def _trust_producer_id(trust: IntegrationTrustManifest | IntegrationCITrust) -> str:
    return (
        str(trust.ci_producer_app_id)
        if isinstance(trust, IntegrationTrustManifest)
        else trust.producer_id
    )


def _payload_producer_id(payload: AttestationPayload | CIReceiptPayload) -> str:
    return (
        str(payload.ci_producer_app_id)
        if isinstance(payload, AttestationPayload)
        else payload.producer_id
    )


def _check_producer_app_id_matches(app_id: int, producer_id: str) -> bool:
    return not producer_id.isdecimal() or app_id == int(producer_id)


def _producer_matches(
    app: dict[str, Any], trust: IntegrationTrustManifest | IntegrationCITrust
) -> bool:
    app_id = _strict_int(app.get("id"))
    if app_id is None or app_id <= 0:
        return False
    expected = _trust_producer_id(trust)
    if expected.isdecimal():
        return app_id == int(expected)
    slug = app.get("slug")
    return isinstance(slug, str) and slug == expected


def _require_workflow_repository(record: dict[str, Any], trust: IntegrationCITrust) -> None:
    repository = record.get("repository")
    head_repository = record.get("head_repository")
    if (
        record.get("status") != "completed"
        or not isinstance(repository, dict)
        or not isinstance(head_repository, dict)
        or _strict_int(repository.get("id")) != trust.repository_id
        or repository.get("full_name") != trust.full_name
        or _strict_int(head_repository.get("id")) != trust.repository_id
        or head_repository.get("full_name") != trust.full_name
    ):
        raise AttestationError("workflow repository identity does not match CI trust")


def _require_latest_attempt_jobs(
    jobs: list[dict[str, Any]],
    *,
    checks: list[dict[str, Any]],
    workflow_run_id: int,
    run_attempt: int,
    head_sha: str,
    full_name: str,
) -> None:
    by_name: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("name")
        job_id = _strict_int(job.get("id"))
        if not isinstance(name, str) or not name or job_id is None or job_id <= 0:
            raise AttestationError("latest workflow attempt job identity is malformed")
        if name in by_name:
            raise AttestationError("latest workflow attempt job identity is ambiguous")
        by_name[name] = job
    for check in checks:
        job = by_name.get(check["name"])
        expected_url = (
            f"https://api.github.com/repos/{full_name}/check-runs/{check['check_run_id']}"
        )
        if (
            job is None
            or _strict_int(job.get("run_id")) != workflow_run_id
            or _strict_int(job.get("run_attempt")) != run_attempt
            or job.get("head_sha") != head_sha
            or job.get("status") != "completed"
            or job.get("conclusion") != check["conclusion"]
            or job.get("check_run_url") != expected_url
        ):
            raise CIObservationDeferred(
                f"required check is not from the latest workflow attempt: {check['name']}",
                classification="superseded",
            )


def _validate_check_workflow_coverage(
    checks: tuple[AttestedCheck, ...],
    workflow_runs: tuple[AttestedWorkflowRun, ...],
    head_sha: str,
) -> None:
    names = [check.name for check in checks]
    check_ids = [check.check_run_id for check in checks]
    suites = [workflow.check_suite_id for workflow in workflow_runs]
    if len(set(names)) != len(names) or len(set(check_ids)) != len(check_ids):
        raise ValueError("CI receipt checks contain duplicates")
    if len(set(suites)) != len(suites):
        raise ValueError("CI receipt workflow attempts contain duplicate suites")
    workflow_by_suite = {workflow.check_suite_id: workflow for workflow in workflow_runs}
    if set(workflow_by_suite) != {check.check_suite_id for check in checks}:
        raise ValueError("CI receipt workflow attempt coverage does not match check suites")
    if any(
        check.head_sha != head_sha
        or workflow_by_suite[check.check_suite_id].head_sha != head_sha
        for check in checks
    ):
        raise ValueError("CI receipt head identity is incoherent")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AttestationError("duplicate attestation field")
        result[key] = value
    return result


def _strict_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class ParentVerification:
    """Shared receipt and trusted-verification invariants for parent subjects."""

    async def diagnose_trusted_binding(
        self, task_id: str, generation: int, head_sha: str
    ) -> dict[str, Any] | None:
        """Why no trusted verification binds this subject, or ``None``.

        The read side of :meth:`complete_parent`'s guard, for callers that must
        decide *before* attempting completion (the aggregate verifier's close).
        It never writes and never reads an unverified worker's output: the
        answer is derived from the checkpoint's own verification columns plus
        the evidence rows recorded for this exact subject, so calling it twice
        on unchanged state returns the same diagnosis.
        """
        async with self.db.immediate() as conn:
            _parent, _project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            if int(checkpoint["generation"]) != generation:
                return None
            diagnosis = binding_diagnosis(
                checkpoint, generation, head_sha, required_checks(operation)
            )
            if diagnosis is None:
                return None
            return diagnosis | await self._awaiting_evidence_detail_on(
                conn, operation["id"], task_id, generation, head_sha
            )


    async def _awaiting_evidence_detail_on(
        self, conn, operation_id: str, task_id: str, generation: int, head_sha: str
    ) -> dict[str, Any]:
        """Name which owner the wait belongs to, from recorded CI evidence.

        "No trusted evidence yet" is three different waits: CI has not run, CI
        ran and reported a failure the repair ladder owns, or CI went green and
        the parent-integration playbook has not accepted its evidence. Only the
        recorded evidence for this exact subject can tell them apart, and the
        next action differs in all three.
        """
        rows = (
            await conn.execute(
                select(
                    integration_check_evidence.c.id,
                    integration_check_evidence.c.conclusion,
                    integration_check_evidence.c.classification,
                    integration_check_evidence.c.producer_id,
                )
                .where(
                    integration_check_evidence.c.operation_id == operation_id,
                    integration_check_evidence.c.parent_task_id == task_id,
                    integration_check_evidence.c.parent_generation == generation,
                    integration_check_evidence.c.parent_head_sha == head_sha,
                )
                .order_by(
                    integration_check_evidence.c.observed_at, integration_check_evidence.c.id
                )
            )
        ).mappings().all()
        detail: dict[str, Any] = {
            "recorded_evidence_ids": [row["id"] for row in rows],
            "recorded_conclusions": sorted({row["conclusion"] for row in rows}),
            "recorded_producer_ids": sorted({row["producer_id"] for row in rows}),
            "recorded_classifications": sorted({row["classification"] for row in rows}),
        }
        if not rows:
            return detail
        conclusions = set(detail["recorded_conclusions"])
        if conclusions != {"success"}:
            detail["next_owner"] = "parent_repair_ladder"
            detail["next_action"] = (
                "Trusted check evidence for this generation and head already records "
                f"{', '.join(sorted(conclusions))}: the aggregate did not pass its required "
                "checks, so the repair ladder owns the next step. Read its active stage "
                "with `aq integration status <project_id>`; a new aggregate needs "
                "a new CI run, not a re-run of the local suite."
            )
            return detail
        detail["next_owner"] = "parent_integration_playbook"
        detail["next_action"] = (
            "Trusted check evidence for this generation and head is already green but no "
            "verification binds it: the parent-integration playbook's verify rule has to "
            "accept it with `integration_parent_verify`, whose own outcome says why it "
            "did not. Re-running the local suite changes nothing."
        )
        return detail


    @parent_engine_guard(outcome="stale_generation")
    async def verify_parent(
        self, task_id: str, generation: int, head_sha: str, evidence_ids: list[str]
    ) -> dict[str, Any]:
        if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
            return {"outcome": "invalid_evidence", "task_id": task_id}
        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            if int(checkpoint["generation"]) != generation:
                return {"outcome": "stale_generation", "task_id": task_id}
            readiness = await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )
            if readiness["outcome"] != "ready":
                return readiness
            if readiness["head_sha"] != head_sha:
                return {"outcome": "stale_head", "task_id": task_id}
            evidence = [
                dict(row)
                for row in (
                    await conn.execute(
                        select(integration_check_evidence).where(
                            integration_check_evidence.c.id.in_(evidence_ids)
                        )
                    )
                ).mappings().all()
            ]
            policy = HierarchicalIntegrationPolicy.model_validate(operation["policy_snapshot"])
            required = policy.parent.required_checks
            valid = len(evidence) == len(evidence_ids)
            covered: set[str] = set()
            for row in evidence:
                valid = valid and all(
                    (
                        row["operation_id"] == operation["id"],
                        row["parent_task_id"] == task_id,
                        row["parent_generation"] == generation,
                        row["parent_head_sha"] == head_sha,
                        row["producer_id"] == required.producer_id,
                        row["required_check_version"] == required.version,
                        row["conclusion"] == "success",
                        row["classification"] != "infrastructure",
                    )
                )
                covered.update(
                    name for name, result in (row["checks"] or {}).items() if result == "success"
                )
            valid = valid and covered == set(required.names)
            if not valid:
                return {"outcome": "invalid_evidence", "task_id": task_id}
            existing = (
                await conn.execute(
                    select(integration_parent_verifications).where(
                        integration_parent_verifications.c.operation_id == operation["id"],
                        integration_parent_verifications.c.generation == generation,
                        integration_parent_verifications.c.head_sha == head_sha,
                    )
                )
            ).mappings().one_or_none()
            verification_id = existing["id"] if existing else str(uuid.uuid4())
            if existing is None:
                await conn.execute(
                    insert(integration_parent_verifications).values(
                        id=verification_id,
                        operation_id=operation["id"],
                        parent_task_id=task_id,
                        episode_id=checkpoint["episode_id"],
                        generation=generation,
                        head_sha=head_sha,
                        required_check_version=required.version,
                        created_at=self.clock(),
                    )
                )
                for evidence_id in sorted(evidence_ids):
                    await conn.execute(
                        insert(integration_parent_verification_evidence).values(
                            verification_id=verification_id,
                            evidence_id=evidence_id,
                        )
                    )
            else:
                linked = set(
                    (
                        await conn.execute(
                            select(integration_parent_verification_evidence.c.evidence_id).where(
                                integration_parent_verification_evidence.c.verification_id
                                == verification_id
                            )
                        )
                    ).scalars().all()
                )
                if linked != set(evidence_ids):
                    return {"outcome": "invalid_evidence", "task_id": task_id}
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .values(
                    checkpoint_sha=head_sha,
                    verified_sha=head_sha,
                    verified_generation=generation,
                    current_verification_id=verification_id,
                    state="verifying",
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
            await enqueue_integration_event(
                conn,
                event_id=f"parent-verified-{verification_id}",
                dedup_key=f"task.integration_verified:{verification_id}",
                project_id=parent["project_id"],
                event_type="task.integration_verified",
                payload={
                    "project_id": parent["project_id"],
                    "operation_id": operation["id"],
                    "task_id": task_id,
                    "title": parent["title"],
                    "generation": generation,
                    "head_sha": head_sha,
                    "verification_id": verification_id,
                },
                available_at=self.clock(),
            )
            return {
                "outcome": "verified",
                "task_id": task_id,
                "generation": generation,
                "head_sha": head_sha,
                "verification_id": verification_id,
            }


    @parent_engine_guard()
    async def wake_verifier(self, task_id: str, fence) -> dict[str, Any]:
        """Wake only the exact transferred verifier on the collected head."""
        from src.database.queries.task_queries import _INTEGRATION_WAKE_TOKEN
        from src.models import TaskStatus

        async with self.db.immediate() as conn:
            parent, project, checkpoint, operation = await self._locked_context_on(
                conn, task_id
            )
            readiness = await self.readiness_on(
                conn,
                parent=parent,
                project=project,
                checkpoint=checkpoint,
                operation=operation,
            )
            if readiness["outcome"] != "ready":
                return readiness
            expected_owner = operation["verifier_task_id"] or task_id
            owner = (
                await conn.execute(
                    select(integration_branch_owners).where(
                        integration_branch_owners.c.repository_id
                        == checkpoint["repository_id"],
                        integration_branch_owners.c.ref == checkpoint["branch"],
                    )
                )
            ).mappings().one_or_none()
            if (
                fence.owner_id != expected_owner
                or fence.target.repository_id != checkpoint["repository_id"]
                or fence.target.branch != checkpoint["branch"]
                or owner is None
                or owner["owner_id"] != expected_owner
                or owner["owner_role"] != "verifier"
                or int(owner["fence_token"]) != fence.token
                or owner["handoff_state"] != "reserved"
            ):
                raise HierarchyError("invariant_error", "verifier handoff is not current")
            wake_task_id = expected_owner if operation["verifier_task_id"] else task_id
            if (
                await self.db._read_manual_pause(conn, task_id) is not None
                or await self.db._read_manual_pause(conn, wake_task_id) is not None
            ):
                raise HierarchyError("human_required", "operator manual pause is active")
            transition = await self.db._apply_transition(
                conn,
                wake_task_id,
                TaskStatus.READY,
                context="integration_verifier_handoff",
                assigned_agent_id=None,
                _manual_pause_control=True,
                _integration_wake_token=_INTEGRATION_WAKE_TOKEN,
            )
            await conn.execute(
                update(task_integration_checkpoints)
                .where(task_integration_checkpoints.c.task_id == task_id)
                .where(task_integration_checkpoints.c.episode_id == checkpoint["episode_id"])
                .values(
                    checkpoint_sha=readiness["head_sha"],
                    branch_owner_id=expected_owner,
                    state="verifying",
                    version=task_integration_checkpoints.c.version + 1,
                    updated_at=self.clock(),
                )
            )
        await self.db.log_blocked_flips(transition.flipped)
        await self.db._notify_ready(transition.ready)
        return readiness | {"outcome": "woken", "owner_id": expected_owner}
