"""Strict trusted CI manifest, attestation, and durable evidence contracts."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import insert, select

from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_repair_operations,
    projects,
    task_integration_checkpoints,
    tasks,
)

ATTESTATION_CHECK_NAME = "Agent Queue Integration Attestation"
TRUST_MANIFEST_PATH = ".github/agent-queue-integration.json"
_SHA_PATTERN = r"^[0-9a-f]{40}$"


class AttestationError(ValueError):
    pass


class RequiredChecksManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    names: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_nonempty_names(self) -> "RequiredChecksManifest":
        if any(not name.strip() for name in self.names) or len(set(self.names)) != len(self.names):
            raise ValueError("required check names must be unique and non-empty")
        return self


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


def select_trusted_attestation(
    records: list[dict[str, Any]],
    trust: IntegrationTrustManifest,
    *,
    expected_head_sha: str,
) -> AttestationPayload:
    trusted: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        app = record.get("app")
        if (
            record.get("name") == trust.attestation_name
            and isinstance(app, dict)
            and _strict_int(app.get("id")) == trust.attestation_app_id
        ):
            record_id = _strict_int(record.get("id"))
            if record_id is None or record_id <= 0:
                raise AttestationError("trusted attestation ordering identity is malformed")
            trusted.append((record_id, record))
    if not trusted:
        raise AttestationError("trusted attestation is missing")
    _, newest = max(trusted, key=lambda item: item[0])
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
        return payload
    except AttestationError:
        raise
    except Exception as exc:
        raise AttestationError("newest trusted attestation is invalid") from exc


@dataclass(frozen=True)
class TrustedCIObservation:
    payload: AttestationPayload
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
    checks: dict[str, Literal["success", "failure", "cancelled", "skipped", "neutral"]]
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
        self, trust: IntegrationTrustManifest, head_sha: str
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

    def __init__(self, db: Any, trust: IntegrationTrustManifest, observer: Any, *, clock=time.time):
        if not isinstance(observer, (AuthenticatedGitHubObserver, TrustedFixtureObserver)):
            raise TypeError("CIService requires an authenticated or explicit fixture observer")
        if isinstance(observer, AuthenticatedGitHubObserver):
            client = observer.client
            if (
                client.config.app_id != trust.attestation_app_id
                or client.repository.repository_id != trust.repository_id
                or client.repository.full_name != trust.full_name
                or client.repository.forge_host != "github.com"
            ):
                raise ValueError("authenticated provider identity does not match trust manifest")
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
            return {
                "outcome": "green" if isinstance(observation, TrustedCIObservation) else "red",
                "evidence_ids": evidence_ids,
            }

    async def _lock_parent_subject_on(
        self, conn: Any, subject: ParentCISubject
    ) -> IntegrationTrustManifest | None:
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
            and candidate["state"] in {"built", "testing"}
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
        trust: IntegrationTrustManifest,
    ) -> list[str]:
        expected_head = (
            subject.head_sha if isinstance(subject, ParentCISubject) else subject.candidate_sha
        )
        if isinstance(observation, TrustedCIObservation):
            payload = observation.payload
            _require_payload_matches_trust(payload, trust, expected_head)
            producer_id = payload.ci_producer_app_id
            version = payload.required_check_set_version
            check_rows = [check.model_dump() for check in payload.checks]
            workflow_rows = [workflow.model_dump() for workflow in payload.workflow_runs]
            overall_conclusion = "success"
        else:
            producer_id = trust.ci_producer_app_id
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
    ) -> IntegrationTrustManifest | None:
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
            or configured.get("producer_id") != str(self.trust.ci_producer_app_id)
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

    def __init__(self, client: Any):
        self.client = client

    async def observe(
        self, trust: IntegrationTrustManifest, head_sha: str
    ) -> TrustedCIObservation | FailedCIObservation:
        if not isinstance(head_sha, str) or re.fullmatch(_SHA_PATTERN, head_sha) is None:
            raise AttestationError("invalid CI head")
        owner, repository = trust.full_name.split("/", 1)
        selected: list[dict[str, Any]] = []
        for name in trust.required_checks.names:
            path = (
                f"/repos/{owner}/{repository}/commits/{head_sha}/check-runs"
                f"?check_name={quote(name, safe='')}&filter=all&per_page=100"
            )
            records = await self.client.paged_items(path, key="check_runs")
            candidates: list[tuple[int, dict[str, Any]]] = []
            for record in records:
                app = record.get("app")
                if (
                    record.get("name") == name
                    and isinstance(app, dict)
                    and _strict_int(app.get("id")) == trust.ci_producer_app_id
                ):
                    record_id = _strict_int(record.get("id"))
                    if record_id is None or record_id <= 0:
                        raise AttestationError(
                            f"required check ordering identity is malformed: {name}"
                        )
                    candidates.append((record_id, record))
            if not candidates:
                raise AttestationError(f"required check is missing: {name}")
            _, newest = max(candidates, key=lambda item: item[0])
            suite = newest.get("check_suite")
            suite_id = _strict_int(suite.get("id")) if isinstance(suite, dict) else None
            conclusion = newest.get("conclusion")
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
                    "producer_app_id": trust.ci_producer_app_id,
                    "head_sha": head_sha,
                    "conclusion": conclusion,
                }
            )

        workflow_records = await self.client.paged_items(
            f"/repos/{owner}/{repository}/actions/runs?head_sha={head_sha}&per_page=100",
            key="workflow_runs",
        )
        workflow_rows: list[dict[str, Any]] = []
        workflow_ids: dict[int, int] = {}
        for suite_id in dict.fromkeys(check["check_suite_id"] for check in selected):
            matches = [
                record
                for record in workflow_records
                if _strict_int(record.get("check_suite_id")) == suite_id
            ]
            if len(matches) != 1:
                raise AttestationError("workflow attempt identity is missing or ambiguous")
            record = matches[0]
            workflow_run_id = _strict_int(record.get("id"))
            workflow_id = _strict_int(record.get("workflow_id"))
            run_attempt = _strict_int(record.get("run_attempt"))
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
        if any(check["conclusion"] != "success" for check in selected) or any(
            workflow["conclusion"] != "success" for workflow in workflow_rows
        ):
            conclusions = {check["conclusion"] for check in selected} | {
                workflow["conclusion"] for workflow in workflow_rows
            }
            overall = "cancelled" if "cancelled" in conclusions else "failure"
            return FailedCIObservation(
                checks=tuple(selected),
                workflow_runs=tuple(workflow_rows),
                workflow_ids=workflow_ids,
                conclusion=overall,
            )
        payload = AttestationPayload(
            schema="aq.integration-attestation.v1",
            canonical_repository_id=trust.canonical_repository_id,
            repository_id=trust.repository_id,
            ci_producer_app_id=trust.ci_producer_app_id,
            attestation_app_id=trust.attestation_app_id,
            head_sha=head_sha,
            required_check_set_version=trust.required_checks.version,
            checks=tuple(AttestedCheck.model_validate(check) for check in selected),
            workflow_runs=tuple(
                AttestedWorkflowRun.model_validate(workflow) for workflow in workflow_rows
            ),
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
            if (
                record.get("name") == trust.attestation_name
                and isinstance(app, dict)
                and _strict_int(app.get("id")) == trust.attestation_app_id
            ):
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
    payload: AttestationPayload,
    trust: IntegrationTrustManifest,
    expected_head_sha: str,
) -> None:
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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AttestationError("duplicate attestation field")
        result[key] = value
    return result


def _strict_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
