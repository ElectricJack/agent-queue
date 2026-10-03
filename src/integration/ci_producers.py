"""Hosted and detached-local producers of the same exact-head CI facts.

Producers report facts; policy decides whether to retry or count an attempt.
Local execution belongs to the existing job runner, never to this adapter.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.database.tables import jobs
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialMode,
    credential_identity_from_client,
)
from src.git.manager import GitError
from src.integration.ci import (
    AttestationError,
    AuthenticatedGitHubObserver,
    CIObservationDeferred,
    IntegrationCITrust,
    IntegrationTrustManifest,
    TrustedCIObservation,
    is_numeric_producer_id,
)
from src.integration.subjects import CIEvidence, CIState, HeadIdentity, Subject
from src.jobs.adapters import finite_command
from src.jobs.policy import JobError, TERMINAL


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ProducerRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["requested", "already_running", "unavailable"]
    request_id: str | None = None
    requested_at: float | None = None
    reason: str | None = None
    job_ids: tuple[str, ...] = ()


class ProducerObservation(CIEvidence):
    """Canonical evidence; cancellation and supersession are never attempts."""

    required_check_version: str
    classification: Literal[
        "conclusive", "cancelled", "superseded", "infra", "pending", "none", "untrusted"
    ]
    trusted: bool = False
    checks: dict[str, str] = Field(default_factory=dict)
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _conclusive(self) -> ProducerObservation:
        if self.state in {CIState.GREEN, CIState.RED}:
            if not self.trusted or self.classification != "conclusive" or not self.evidence_id:
                raise ValueError("green/red requires trusted conclusive identified evidence")
            if not self.checks:
                raise ValueError("conclusive evidence needs checks")
            if self.state is CIState.GREEN and any(v != "success" for v in self.checks.values()):
                raise ValueError("green requires every check to succeed")
        elif self.classification == "conclusive":
            raise ValueError("only green/red is conclusive")
        return self

    @property
    def is_attempt(self) -> bool:
        return self.trusted and self.classification == "conclusive"


class CIProducer(Protocol):
    required_check_version: str

    async def request(self, subject: Subject, head: HeadIdentity) -> ProducerRequest: ...

    async def observe(self, subject: Subject, head: HeadIdentity) -> ProducerObservation: ...


HostedRequester = Callable[[Subject, HeadIdentity], Awaitable[ProducerRequest]]


class HostedCIProducer:
    """Keep authenticated observation and App trust on the existing path.

    ``requester`` is an existing journaled publication/dispatch command, not
    a raw git callback. With no requester, observation remains available.
    App-mode trust must be loaded from the exact subject tree by the existing
    attestation service, not synthesized from policy fields.
    """

    def __init__(
        self,
        client: Any,
        trust: IntegrationCITrust | IntegrationTrustManifest,
        *,
        requester: HostedRequester | None = None,
        expected_event: str | None = "push",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client, self.trust = client, trust
        self.requester, self.clock = requester, clock
        self.observer = AuthenticatedGitHubObserver(client, expected_event=expected_event)
        self.required_check_version = trust.required_checks.version

    def _trust_error(self, head: HeadIdentity) -> str | None:
        if head.repository_id != self.trust.canonical_repository_id:
            return "repository_mismatch"
        try:
            identity = credential_identity_from_client(self.client)
        except ValueError:
            return "credential_identity_unavailable"
        if identity.mode is GitHubCredentialMode.APP:
            if not isinstance(self.trust, IntegrationTrustManifest):
                return "app_subject_manifest_required"
            if identity.app_id != self.trust.attestation_app_id:
                return "attestation_app_mismatch"
            if not is_numeric_producer_id(str(self.trust.ci_producer_app_id)):
                return "numeric_producer_required"
        return None

    async def request(self, subject: Subject, head: HeadIdentity) -> ProducerRequest:
        reason = self._trust_error(head)
        if reason or self.requester is None:
            return ProducerRequest(outcome="unavailable", reason=reason or "requester_unavailable")
        try:
            return await self.requester(subject, head)
        except (GitHubAccessError, GitError, OSError) as exc:
            return ProducerRequest(
                outcome="unavailable",
                reason=exc.category
                if isinstance(exc, GitHubAccessError)
                else "request_unavailable",
            )

    async def observe(self, subject: Subject, head: HeadIdentity) -> ProducerObservation:
        producer = (
            str(self.trust.ci_producer_app_id)
            if isinstance(self.trust, IntegrationTrustManifest)
            else self.trust.producer_id
        )
        common = dict(
            head_sha=head.sha,
            producer=producer,
            required_check_version=self.required_check_version,
            observed_at=self.clock(),
        )
        reason = self._trust_error(head)
        if reason:
            return ProducerObservation(
                **common,
                state=CIState.UNTRUSTED,
                classification="untrusted",
                reason=reason,
            )
        try:
            observed = await self.observer.observe(self.trust, head.sha)
        except CIObservationDeferred as exc:
            state = {
                "none": CIState.NONE,
                "pending": CIState.PENDING,
                "superseded": CIState.INFRA,
                "infra": CIState.INFRA,
            }[exc.classification]
            return ProducerObservation(
                **common,
                state=state,
                classification=exc.classification,
                reason=str(exc),
            )
        except (AttestationError, ValueError) as exc:
            return ProducerObservation(
                **common,
                state=CIState.UNTRUSTED,
                classification="untrusted",
                reason=str(exc),
            )
        except (GitHubAccessError, OSError) as exc:
            return ProducerObservation(
                **common,
                state=CIState.INFRA,
                classification="infra",
                reason=exc.category
                if isinstance(exc, GitHubAccessError)
                else "transport_unavailable",
            )
        if isinstance(observed, TrustedCIObservation):
            checks = {check.name: check.conclusion for check in observed.payload.checks}
            identity = observed.payload.external_id
            classification, state = "conclusive", CIState.GREEN
            details = {"receipt": observed.payload.model_dump(mode="json")}
        else:
            checks = {check["name"]: check["conclusion"] for check in observed.checks}
            details = {
                "checks": list(observed.checks),
                "workflow_runs": list(observed.workflow_runs),
            }
            identity = "hosted:" + digest(details)
            classification, state = (
                ("cancelled", CIState.INFRA)
                if observed.conclusion == "cancelled"
                else ("conclusive", CIState.RED)
                if "failure" in checks.values()
                or "missing" in checks.values()
                or any(run["conclusion"] == "failure" for run in observed.workflow_runs)
                else ("infra", CIState.INFRA)
            )
        return ProducerObservation(
            **common,
            state=state,
            evidence_id=identity,
            classification=classification,
            trusted=True,
            checks=checks,
            details=details,
        )


class LocalValidationPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    # An explicit policy-supplied identity, changed only to request a new attempt.
    attempt_id: str = Field(min_length=1)
    commands: tuple[str, ...] = ()
    queue_seconds: float = Field(default=1800, ge=0)
    run_seconds: float = Field(default=300, gt=0)


JobReader = Callable[[Subject, tuple[str, ...]], Awaitable[dict[str, dict[str, Any]]]]


class LocalCIProducer:
    """Sequential finite presets in a durable detached snapshot, with no waits."""

    def __init__(
        self,
        db: Any,
        job_client: Any,
        *,
        store: str | Path,
        plan: LocalValidationPlan,
        read_jobs: JobReader | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.job_client, self.store = db, job_client, str(store)
        self.plan, self.clock = plan, clock
        self.required_check_version = plan.version
        self.read_jobs = read_jobs or self._read_jobs

    def request_identity(self, subject: Subject, head: HeadIdentity) -> str:
        return "local-ci:" + digest(
            {
                "subject": subject.id,
                "project": subject.project_id,
                "head": head.model_dump(mode="json"),
                "policy": subject.policy.model_dump(mode="json"),
                "plan": self.plan.model_dump(mode="json"),
            }
        )

    def _keys(self, subject: Subject, head: HeadIdentity) -> tuple[str, ...]:
        identity = self.request_identity(subject, head)
        return tuple(f"{identity}:{index}" for index in range(len(self.plan.commands)))

    async def _read_jobs(
        self, subject: Subject, keys: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(jobs).where(
                            jobs.c.project_id == subject.project_id,
                            jobs.c.owner_kind == "integration",
                            jobs.c.owner_id == subject.id,
                            jobs.c.idempotency_key.in_(keys),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {row["idempotency_key"]: dict(row) for row in rows}

    async def request(self, subject: Subject, head: HeadIdentity) -> ProducerRequest:
        identity = self.request_identity(subject, head)
        if self.job_client is None:
            return ProducerRequest(
                outcome="unavailable", reason="jobs.disabled", request_id=identity
            )
        if not self.plan.commands:
            return ProducerRequest(
                outcome="unavailable", reason="validation_not_run", request_id=identity
            )
        # Validate the whole finite plan before submitting any part of it.
        try:
            commands = tuple(finite_command(command) for command in self.plan.commands)
            keys = self._keys(subject, head)
            existing = await self.read_jobs(subject, keys)
            job_ids = []
            for key, (preset, argv) in zip(keys, commands, strict=True):
                job = existing.get(key)
                if job is None:
                    job = await self.job_client.submit(
                        project_id=subject.project_id,
                        operation_id=subject.id,
                        store=self.store,
                        input_ref=head.sha,
                        preset=preset,
                        argv=argv,
                        snapshot_group=identity,
                        idempotency_key=key,
                        queue_seconds=self.plan.queue_seconds,
                        run_seconds=self.plan.run_seconds,
                    )
                    return ProducerRequest(
                        outcome="requested",
                        request_id=identity,
                        requested_at=job.get("submitted_at"),
                        job_ids=(*job_ids, job["id"]),
                    )
                job_ids.append(job["id"])
                state, _, _, reason = self._classify_job(subject, head, job)
                if state is not CIState.GREEN:
                    return ProducerRequest(
                        outcome="already_running" if state is CIState.PENDING else "unavailable",
                        request_id=identity,
                        requested_at=job.get("submitted_at"),
                        job_ids=tuple(job_ids),
                        reason=reason,
                    )
            return ProducerRequest(
                outcome="already_running",
                request_id=identity,
                job_ids=tuple(job_ids),
                requested_at=min(job["submitted_at"] for job in existing.values()),
                reason="validation_complete",
            )
        except JobError as exc:
            return ProducerRequest(outcome="unavailable", request_id=identity, reason=str(exc))

    @staticmethod
    def _classify_job(subject: Subject, head: HeadIdentity, job: dict) -> tuple:
        if (
            job.get("owner_kind") != "integration"
            or job.get("owner_id") != subject.id
            or job.get("project_id") != subject.project_id
            or job.get("input_mode") != "snapshot"
            or job.get("input_ref") != head.sha
        ):
            return CIState.UNTRUSTED, "untrusted", "missing", "job_identity_mismatch"
        if job["state"] not in TERMINAL:
            return CIState.PENDING, "pending", "pending", None
        result = job.get("result")
        if not isinstance(result, dict):
            return CIState.INFRA, "infra", "missing", "jobs.result_missing"
        if (
            result.get("job_id") != job["id"]
            or result.get("state") != job["state"]
            or result.get("input_mode") != "snapshot"
            or result.get("input_ref") != head.sha
            or result.get("result_hash")
            != digest({k: v for k, v in result.items() if k != "result_hash"})
        ):
            return CIState.UNTRUSTED, "untrusted", "missing", "result_identity_mismatch"
        outcome = result.get("outcome")
        if outcome == "cancelled":
            return CIState.INFRA, "cancelled", "cancelled", "cancelled"
        if outcome in {"infrastructure", "lost"}:
            return CIState.INFRA, "infra", "missing", result.get("infra_reason") or outcome
        if result.get("input_stability") != "stable":
            return CIState.INFRA, "infra", "missing", "snapshot_modified"
        if outcome == "failed":
            return CIState.RED, "conclusive", "failure", None
        if (
            outcome == "passed"
            and job["state"] == "succeeded"
            and type(result.get("exit_code")) is int
            and result["exit_code"] == 0
            and result.get("signal") is None
            and result.get("infra_reason") is None
        ):
            return CIState.GREEN, "conclusive", "success", None
        return CIState.INFRA, "infra", "missing", "invalid_job_conclusion"

    async def observe(self, subject: Subject, head: HeadIdentity) -> ProducerObservation:
        keys = self._keys(subject, head)
        existing = await self.read_jobs(subject, keys)
        requested_at = min((job["submitted_at"] for job in existing.values()), default=None)
        now = self.clock()
        common = dict(
            head_sha=head.sha,
            producer="local-jobs",
            required_check_version=self.plan.version,
            observed_at=now,
            requested_at=requested_at,
            age_seconds=max(0, now - requested_at) if requested_at is not None else None,
        )
        if not keys or not existing:
            return ProducerObservation(**common, state=CIState.NONE, classification="none")
        checks, classified, details = {}, [], []
        for index, key in enumerate(keys):
            job = existing.get(key)
            if job is None:
                checks[str(index)] = "missing"
                classified.append((CIState.PENDING, "pending", "missing", None))
                continue
            state = self._classify_job(subject, head, job)
            classified.append(state)
            checks[str(index)] = state[2]
            details.append({"job_id": job["id"], "result": job.get("result")})
        # A real failure outranks infrastructure, but identity failures cannot
        # be used as evidence for any attempt.
        selected = next((item for item in classified if item[0] is CIState.UNTRUSTED), None)
        for wanted in (CIState.RED, CIState.INFRA, CIState.PENDING, CIState.GREEN):
            if selected is None:
                selected = next((item for item in classified if item[0] is wanted), None)
        state, classification, _, reason = selected
        return ProducerObservation(
            **common,
            state=state,
            classification=classification,
            checks=checks,
            reason=reason,
            evidence_id="local:" + digest(details),
            trusted=state is not CIState.UNTRUSTED,
            details={"request_id": self.request_identity(subject, head), "jobs": details},
        )
