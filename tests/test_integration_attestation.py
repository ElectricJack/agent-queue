from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError

from src.database.tables import (
    integration_attestation_publications,
    integration_batches,
    integration_candidate_publications,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_outbox,
    integration_repair_operations,
    integration_repair_stages,
)
from src.git.github_app import GitHubAppError, GitHubRepositoryBinding
from src.git.github_contracts import GitHubCredentialIdentity
from src.integration.attestation import IntegrationAttestationService
from src.integration.ci import (
    ATTESTATION_CHECK_NAME,
    AttestationError,
    AttestationPayload,
    IntegrationTrustManifest,
    SubjectTrustError,
)
from src.integration.main_promotion import RootAttestationSubject
from src.integration.ci_producers import HostedCIProducer, LocalCIProducer
from src.integration.repair import RepairService
from src.models import Project, RepoConfig, RepoSourceType

SHA = "a" * 40
BASE = "0" * 40


async def test_train_publish_without_legacy_rows_reads_back_and_retries_idempotently(tmp_path):
    client = ProviderClient()
    service = IntegrationAttestationService(
        None, data_dir=tmp_path, git_manager=None, github_client_factory=None,
    )
    producer = HostedCIProducer(client, IntegrationTrustManifest.model_validate_json(trust_document()))
    target = subject(operation_id="train-batch", batch_id="train-batch", revision=3)
    for _ in range(2):
        result = await service.publish(target, producer=producer)
        assert result.outcome in {"published", "already_published"}
        assert result.proof.subject() == target
        assert result.proof.check_run_id == client.records[0]["id"]
    assert client.published == 1


@pytest.mark.parametrize("failure", ["local", "login", "wrong_app", "version", "red", "write", "readback"])
async def test_train_publish_refuses_invalid_identity_or_provider_failure(tmp_path, failure):
    client = ProviderClient()
    trust = IntegrationTrustManifest.model_validate_json(trust_document())
    service = IntegrationAttestationService(
        None, data_dir=tmp_path, git_manager=None, github_client_factory=None,
    )
    producer = HostedCIProducer(client, trust)
    target = subject()
    if failure == "local":
        producer = object.__new__(LocalCIProducer)
    elif failure == "login":
        client.credential_identity = GitHubCredentialIdentity.existing_login()
    elif failure == "wrong_app":
        client.credential_identity = GitHubCredentialIdentity.app(999, 202)
    elif failure == "version":
        target = subject(required_check_version="wrong-version")
    elif failure == "red":
        client._required = lambda *args: {**ProviderClient._required(*args), "conclusion": "failure"}
    elif failure == "write":
        client.request_json = AsyncMock(side_effect=OSError("write unavailable"))
    elif failure == "readback":
        original = client.paged_items

        async def unreadable(path, *, key):
            if client.published and "check_name=Agent%20Queue" in path:
                return []
            return await original(path, key=key)

        client.paged_items = unreadable
    result = await service.publish(target, producer=producer)
    assert result.outcome == {"version": "stale", "red": "not_green"}.get(
        failure, "configuration_blocked",
    )
    assert result.proof is None
    assert client.published == (1 if failure == "readback" else 0)


def trust_document(**changes) -> bytes:
    value = {
        "schema": "aq.integration-trust.v1",
        "canonical_repository_id": "repo-config-1",
        "repository_id": 303,
        "full_name": "acme/widgets",
        "ci_producer_app_id": 404,
        "attestation_app_id": 101,
        "attestation_name": ATTESTATION_CHECK_NAME,
        "required_checks": {
            "version": "checks-v1",
            "names": ["Tests (default)", "Tests (postgres-integration)"],
        },
    }
    value.update(changes)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def subject(**changes) -> RootAttestationSubject:
    value = {
        "repository_numeric_id": 303,
        "repository_full_name": "acme/widgets",
        "operation_id": "root-op",
        "batch_id": "batch",
        "revision": 0,
        "candidate_sha": SHA,
        "required_check_version": "checks-v1",
    }
    value.update(changes)
    return RootAttestationSubject.model_validate(value)


def attestation_payload() -> AttestationPayload:
    return AttestationPayload.model_validate(
        {
            "schema": "aq.integration-attestation.v1",
            "canonical_repository_id": "repo-config-1",
            "repository_id": 303,
            "ci_producer_app_id": 404,
            "attestation_app_id": 101,
            "head_sha": SHA,
            "required_check_set_version": "checks-v1",
            "checks": [
                {
                    "name": "Tests (default)",
                    "check_run_id": 11,
                    "check_suite_id": 21,
                    "producer_app_id": 404,
                    "head_sha": SHA,
                    "conclusion": "success",
                },
                {
                    "name": "Tests (postgres-integration)",
                    "check_run_id": 12,
                    "check_suite_id": 22,
                    "producer_app_id": 404,
                    "head_sha": SHA,
                    "conclusion": "success",
                },
            ],
            "workflow_runs": [
                {
                    "workflow_run_id": 31,
                    "run_attempt": 2,
                    "check_suite_id": 21,
                    "head_sha": SHA,
                    "conclusion": "success",
                },
                {
                    "workflow_run_id": 32,
                    "run_attempt": 1,
                    "check_suite_id": 22,
                    "head_sha": SHA,
                    "conclusion": "success",
                },
            ],
        }
    )


@pytest.fixture
async def attestation_db(tmp_path, reuse_database):
    db = await reuse_database("attestation.db")
    await db.create_project(Project(id="p", name="project"))
    await db.create_repo(
        RepoConfig(
            id="repo-config-1",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
        )
    )
    await db.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo-config-1",
    )
    required = {
        "version": "checks-v1",
        "names": ["Tests (default)", "Tests (postgres-integration)"],
        "producer_id": "404",
    }
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo-config-1",
                request_id="request",
                source_manifest_digest="sha256:" + "d" * 64,
                base_sha=BASE,
                lifecycle="testing",
                current_revision=0,
                integration_branch="refs/heads/aq/integration/batch",
                policy_snapshot={"root": {"required_checks": required}},
                artifact_snapshot={},
                tested_candidate_sha=SHA,
                ci_evidence_id="ci-aggregate",
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch",
                revision=0,
                construction_base_sha=BASE,
                next_member_ordinal=1,
                head_sha=SHA,
                state="green",
                ci_evidence_id="ci-aggregate",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="root-op",
                target_kind="batch",
                batch_id="batch",
                episode_id="batch",
                active_stage=0,
                state="active",
                policy_snapshot={"root": {"required_checks": required}},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="root-op",
                ordinal=0,
                policy={},
                state="awaiting_completion",
                intelligence_class="deep",
                starting_sha=SHA,
                deadline_at=1000.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_publications).values(
                batch_id="batch",
                revision=0,
                state="pr_published",
                repository_id="repo-config-1",
                repository_numeric_id=303,
                repository_full_name="acme/widgets",
                base_ref="main",
                head_ref="aq/integration/batch",
                head_sha=SHA,
                expected_old_sha=BASE,
                idempotency_key="publication",
                pr_number=7,
                pr_url="https://github.com/acme/widgets/pull/7",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="ci-aggregate",
                operation_id="root-op",
                batch_id="batch",
                candidate_revision=0,
                producer_id="404",
                workflow_id="aggregate:one",
                run_id=attestation_payload().external_id,
                attempt=0,
                required_check_version="checks-v1",
                checks={
                    "Tests (default)": "success",
                    "Tests (postgres-integration)": "success",
                },
                conclusion="success",
                classification="conclusive",
                observed_at=1.0,
            )
        )
    yield db


class ExactTreeGit:
    def __init__(self, manifest: bytes | None):
        # ``None`` is a subject tree that predates the manifest.
        self.manifest = manifest
        self.fetches: list[dict] = []

    async def afetch_repository_oid(self, destination_git_dir, **kwargs):
        self.fetches.append({"destination_git_dir": destination_git_dir, **kwargs})
        return kwargs["oid"]

    async def arun_git_result(self, args, **kwargs):
        assert args == ["show", f"{SHA}:.github/agent-queue-integration.json"]
        if self.manifest is None:
            return SimpleNamespace(
                returncode=128,
                stdout="",
                stderr="fatal: path '.github/agent-queue-integration.json' does not exist",
            )
        return SimpleNamespace(returncode=0, stdout=self.manifest.decode(), stderr="")


class ProviderClient:
    # API transport is always gh; App trust comes only from this explicit
    # credential identity, never from the historical transport-shaped flag.
    auth_mode = "gh"

    def __init__(self):
        self.credential_identity = GitHubCredentialIdentity.app(101, 202)
        self.repository = GitHubRepositoryBinding(303, "acme/widgets")
        self.records: list[dict] = []
        self.published = 0
        self.workflow_offset = 0

    async def installation_token(self):
        return "dummy-installation-token"

    async def paged_items(self, path, *, key):
        if key == "jobs":
            run_id = int(path.split("/runs/", 1)[1].split("/", 1)[0])
            index = 0 if run_id % 1000 == 31 else 1
            attempt = int(path.split("/attempts/", 1)[1].split("/", 1)[0])
            return [
                {
                    "id": 101 + index,
                    "name": (
                        "Tests (default)"
                        if index == 0
                        else "Tests (postgres-integration)"
                    ),
                    "run_id": run_id,
                    "run_attempt": attempt,
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "success",
                    "check_run_url": (
                        "https://api.github.com/repos/acme/widgets/check-runs/"
                        f"{11 + index}"
                    ),
                }
            ]
        if key == "workflow_runs":
            return [
                {
                    "id": 31 + self.workflow_offset,
                    "workflow_id": 301 + self.workflow_offset,
                    "run_attempt": 2,
                    "check_suite_id": 21,
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "repository": {"id": 303, "full_name": "acme/widgets"},
                    "head_repository": {"id": 303, "full_name": "acme/widgets"},
                },
                {
                    "id": 32 + self.workflow_offset,
                    "workflow_id": 302 + self.workflow_offset,
                    "run_attempt": 1,
                    "check_suite_id": 22,
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "repository": {"id": 303, "full_name": "acme/widgets"},
                    "head_repository": {"id": 303, "full_name": "acme/widgets"},
                },
            ]
        if key == "check_runs" and "check_name=" not in path:
            return [
                self._required("Tests (default)", 11, 21),
                self._required("Tests (postgres-integration)", 12, 22),
            ]
        if "check_name=Tests%20%28default%29" in path:
            return [self._required("Tests (default)", 11, 21)]
        if "check_name=Tests%20%28postgres-integration%29" in path:
            return [self._required("Tests (postgres-integration)", 12, 22)]
        return list(self.records)

    async def request_json(self, method, path, *, json_body, expected_statuses):
        assert method == "POST" and expected_statuses == {201}
        self.published += 1
        record = {
            "id": 7001,
            "app": {"id": 101},
            "head_sha": SHA,
            **json_body,
        }
        self.records.append(record)
        return {"id": 7001}

    @staticmethod
    def _required(name, record_id, suite_id):
        return {
            "id": record_id,
            "name": name,
            "app": {"id": 404},
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
            "check_suite": {"id": suite_id},
        }


class GitHubCLIProviderClient(ProviderClient):
    def __init__(self):
        super().__init__()
        self.credential_identity = GitHubCredentialIdentity.existing_login()

    async def installation_token(self):
        return None

    @staticmethod
    def _required(name, record_id, suite_id):
        return {
            **ProviderClient._required(name, record_id, suite_id),
            "app": {"id": 404, "slug": "404"},
        }


async def _reset_candidate_for_observation(db):
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(tested_candidate_sha=None, ci_evidence_id=None)
        )
        await conn.execute(
            update(integration_candidate_revisions)
            .where(
                integration_candidate_revisions.c.batch_id == "batch",
                integration_candidate_revisions.c.revision == 0,
            )
            .values(state="built", ci_evidence_id=None)
        )
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.operation_id == "root-op",
                integration_repair_stages.c.ordinal == 0,
            )
            .values(state="active")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_kind", "expected_outcome", "expected_event"),
    [
        ("pending", "not_green", None),
        ("missing", "red", "integration.candidate_red"),
        ("red", "red", "integration.candidate_red"),
        ("green", "published", "integration.candidate_green"),
    ],
)
async def test_candidate_observation_emits_only_durable_terminal_ci_continuations(
    attestation_db, tmp_path, provider_kind, expected_outcome, expected_event
):
    await _reset_candidate_for_observation(attestation_db)
    client = ProviderClient()
    client.workflow_offset = 1000
    if provider_kind == "pending":
        original = client.paged_items

        async def pending(path, *, key):
            if key == "check_runs" and "check_name=" not in path:
                return []
            rows = await original(path, key=key)
            if key == "workflow_runs":
                rows[0] = {**rows[0], "status": "in_progress", "conclusion": None}
            return rows

        client.paged_items = pending
    elif provider_kind == "missing":
        original = client.paged_items

        async def missing(path, *, key):
            if key == "check_runs" and "check_name=" not in path:
                return []
            return await original(path, key=key)

        client.paged_items = missing
    elif provider_kind == "red":
        original = client.paged_items

        async def red(path, *, key):
            rows = await original(path, key=key)
            if key == "check_runs" and "check_name=" not in path:
                rows = [
                    {**row, "conclusion": "failure"} if row["name"] == "Tests (default)" else row
                    for row in rows
                ]
            return rows

        client.paged_items = red
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )
    row = {
        "operation_id": "root-op",
        "batch_id": "batch",
        "revision": 0,
        "candidate_sha": SHA,
    }

    first = await service.handle_candidate_ci(row, 10.0)
    if provider_kind == "green":
        async with attestation_db.immediate() as conn:
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == "batch")
                .values(lifecycle="repairing")
            )
    second = await service.handle_candidate_ci(row, 10.0)

    assert first["outcome"] == expected_outcome
    if provider_kind == "pending":
        assert second["outcome"] == "not_green"
    async with attestation_db._engine.connect() as conn:
        events = (await conn.execute(select(integration_outbox))).mappings().all()
        evidence = (await conn.execute(select(integration_check_evidence))).mappings().all()
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "root-op",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
        batch = (
            await conn.execute(
                select(integration_batches).where(integration_batches.c.id == "batch")
            )
        ).mappings().one()
    if provider_kind == "green":
        assert batch["lifecycle"] == "testing"
    if expected_event is None:
        assert events == []
        assert {row["id"] for row in evidence} == {"ci-aggregate"}
    else:
        assert len(events) == 1
        assert events[0]["event_type"] == expected_event
        assert {
            key: events[0]["payload"][key]
            for key in ("project_id", "operation_id", "batch_id", "revision", "head_sha")
        } == {
            "project_id": "p",
            "operation_id": "root-op",
            "batch_id": "batch",
            "revision": 0,
            "head_sha": SHA,
        }
    if provider_kind == "missing":
        missing_evidence = [row for row in evidence if row["id"] != "ci-aggregate"]
        assert len(missing_evidence) == 1
        assert missing_evidence[0]["conclusion"] == "failure"
        assert missing_evidence[0]["checks"] == {
            "Tests (default)": "missing", "Tests (postgres-integration)": "missing"
        }
        assert stage["dossier"]["failed_checks"] == [{
            "evidence_id": missing_evidence[0]["id"],
            "checks": missing_evidence[0]["checks"],
        }]


@pytest.mark.asyncio
async def test_red_candidate_regular_failure_folds_dossier_dedup(
    attestation_db, tmp_path
):
    """A regular CI failure (no missing checks) folds failed_checks and logs
    into the stage dossier before the red event is enqueued."""
    await _reset_candidate_for_observation(attestation_db)
    client = ProviderClient()
    client.workflow_offset = 1000

    original = client.paged_items

    async def red(path, *, key):
        rows = await original(path, key=key)
        if key == "check_runs" and "check_name=" not in path:
            rows = [
                {**row, "conclusion": "failure"} if row["name"] == "Tests (default)" else row
                for row in rows
            ]
        return rows

    client.paged_items = red
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )
    row = {
        "operation_id": "root-op",
        "batch_id": "batch",
        "revision": 0,
        "candidate_sha": SHA,
    }
    result = await service.handle_candidate_ci(row, 10.0)
    assert result["outcome"] == "red"

    async with attestation_db._engine.connect() as conn:
        events = (await conn.execute(select(integration_outbox))).mappings().all()
        evidence = (await conn.execute(select(integration_check_evidence))).mappings().all()
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "root-op",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()

    assert len(events) == 1
    assert events[0]["event_type"] == "integration.candidate_red"

    # Both workflow suites produce a conclusive "failure" row for the red
    # observation; neither is the pre-existing "ci-aggregate" seed.
    red_evidence = [row for row in evidence if row["id"] != "ci-aggregate"]
    assert len(red_evidence) >= 1
    assert all(row["conclusion"] == "failure" for row in red_evidence)
    red_ids = {row["id"] for row in red_evidence}

    dossier = stage["dossier"]
    # failed_checks contains exactly the red evidence ids (deduped, no seed)
    assert {item["evidence_id"] for item in dossier["failed_checks"]} == red_ids
    # logs contains one entry per red evidence row
    assert {item["evidence_id"] for item in dossier["logs"]} == red_ids
    # attempts reflects the counted failures (1 per conclusive failure row)
    assert stage["attempts"] == len(red_evidence)


@pytest.mark.asyncio
async def test_red_candidate_double_observation_dedup_dossier(
    attestation_db, tmp_path
):
    """Two red observations of the same evidence must not double-fold the dossier."""
    await _reset_candidate_for_observation(attestation_db)
    client = ProviderClient()
    client.workflow_offset = 1000

    original = client.paged_items

    async def red(path, *, key):
        rows = await original(path, key=key)
        if key == "check_runs" and "check_name=" not in path:
            rows = [
                {**row, "conclusion": "failure"} if row["name"] == "Tests (default)" else row
                for row in rows
            ]
        return rows

    client.paged_items = red
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )
    row = {
        "operation_id": "root-op",
        "batch_id": "batch",
        "revision": 0,
        "candidate_sha": SHA,
    }
    first = await service.handle_candidate_ci(row, 10.0)
    assert first["outcome"] == "red"

    async with attestation_db._engine.connect() as conn:
        stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "root-op",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
    first_attempts = stage["attempts"]
    first_fc = [item["evidence_id"] for item in stage["dossier"]["failed_checks"]]
    first_lg = [item["evidence_id"] for item in stage["dossier"]["logs"]]

    # Second observation: the stage is no longer "active", so the guard should
    # return stale_subject and NOT re-fold the dossier.
    await service.handle_candidate_ci(row, 10.0)
    async with attestation_db._engine.connect() as conn:
        stage2 = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == "root-op",
                    integration_repair_stages.c.ordinal == 0,
                )
            )
        ).mappings().one()
    assert stage2["attempts"] == first_attempts
    assert [item["evidence_id"] for item in stage2["dossier"]["failed_checks"]] == first_fc
    assert [item["evidence_id"] for item in stage2["dossier"]["logs"]] == first_lg


@pytest.mark.asyncio
async def test_existing_login_candidate_missing_check_emits_red_continuation(
    attestation_db, tmp_path
):
    await _reset_candidate_for_observation(attestation_db)
    client = GitHubCLIProviderClient()
    original = client.paged_items

    async def missing(path, *, key):
        rows = await original(path, key=key)
        if key == "check_runs" and "check_name=" not in path:
            return [row for row in rows if row["name"] != "Tests (postgres-integration)"]
        return rows

    client.paged_items = missing
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )
    observed = await service.handle_candidate_ci(
        {"operation_id": "root-op", "batch_id": "batch", "revision": 0, "candidate_sha": SHA},
        10.0,
    )

    assert observed["outcome"] == "red"
    async with attestation_db._engine.connect() as conn:
        events = (await conn.execute(select(integration_outbox))).mappings().all()
        evidence = (await conn.execute(select(integration_check_evidence))).mappings().all()
    assert len(events) == 1
    assert events[0]["event_type"] == "integration.candidate_red"
    missing_evidence = [row for row in evidence if row["id"] != "ci-aggregate"]
    assert len(missing_evidence) == 1
    assert missing_evidence[0]["checks"] == {
        "Tests (default)": "success", "Tests (postgres-integration)": "missing"
    }


@pytest.mark.asyncio
async def test_gh_candidate_green_receipt_is_durable_and_latest_rerun_fenced(
    attestation_db, tmp_path
):
    await _reset_candidate_for_observation(attestation_db)
    client = GitHubCLIProviderClient()
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )
    row = {
        "operation_id": "root-op",
        "batch_id": "batch",
        "revision": 0,
        "candidate_sha": SHA,
    }

    observed = await service.handle_candidate_ci(row, 10.0)
    proof = await service.resolve(subject())

    assert observed["outcome"] == "published"
    assert proof is not None
    assert proof.check_run_id == 12
    assert proof.external_id.startswith("aq-ci-receipt-v1:")
    assert client.published == 0
    async with attestation_db._engine.connect() as conn:
        publication = (
            await conn.execute(select(integration_attestation_publications))
        ).mappings().one()
    assert publication["state"] == "published"
    assert publication["check_run_id"] == 12
    assert publication["external_id"] == proof.external_id

    assert (await service.publish(subject())).outcome == "already_published"
    client.workflow_offset = 1000
    assert (await service.publish(subject())).outcome == "not_green"
    assert await service.resolve(subject()) is None


@pytest.mark.asyncio
async def test_gh_receipt_restart_finishes_reserved_db_write_without_lease_wait(
    attestation_db, tmp_path
):
    await _reset_candidate_for_observation(attestation_db)
    client = GitHubCLIProviderClient()

    async def crash(phase):
        if phase == "after_publication_reservation":
            raise RuntimeError("simulated daemon loss")

    first = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        crash_hook=crash,
        clock=lambda: 10.0,
    )
    with pytest.raises(RuntimeError, match="simulated daemon loss"):
        await first.handle_candidate_ci(
            {
                "operation_id": "root-op",
                "batch_id": "batch",
                "revision": 0,
                "candidate_sha": SHA,
            },
            10.0,
        )

    restarted = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )
    result = await restarted.publish(subject())

    assert result.outcome == "published"
    assert result.proof is not None
    assert await restarted.resolve(subject()) == result.proof


@pytest.mark.asyncio
async def test_publish_reads_trust_from_authenticated_exact_candidate_oid(attestation_db, tmp_path):
    git = ExactTreeGit(trust_document())
    client = ProviderClient()
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=git,
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )

    result = await service.publish(subject())

    assert result.outcome == "published"
    assert result.proof is not None and result.proof.subject() == subject()
    assert git.fetches[0]["oid"] == SHA
    assert git.fetches[0]["repository"] == GitHubRepositoryBinding(303, "acme/widgets")
    assert client.published == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        b"{not-json",
        trust_document(canonical_repository_id="other"),
        trust_document(repository_id=304),
        trust_document(attestation_app_id=404),
        trust_document(required_checks={"version": "checks-v1", "names": []}),
    ],
)
async def test_publish_fails_closed_for_untrusted_candidate_tree(
    attestation_db, tmp_path, manifest
):
    client = ProviderClient()
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(manifest),
        github_client_factory=lambda binding: client,
    )

    result = await service.publish(subject())

    assert result.outcome == "configuration_blocked"
    assert result.proof is None
    assert client.published == 0
    async with attestation_db._engine.connect() as conn:
        assert await conn.scalar(select(integration_candidate_revisions.c.state)) == "green"


CANDIDATE_ROW = {
    "operation_id": "root-op",
    "batch_id": "batch",
    "revision": 0,
    "candidate_sha": SHA,
}
SNAPSHOT_CHECKS = ("Tests (default)", "Tests (postgres-integration)")


def _recording(client):
    """Record every required-check listing the observer asks the provider for."""
    asked: list[str] = []
    original = client.paged_items

    async def paged_items(path, *, key):
        if key == "check_runs" and "check_name=" not in path:
            asked.append(path)
        return await original(path, key=key)

    client.paged_items = paged_items
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tree_checks",
    [
        {"version": "checks-v0", "names": ["Tests (default)"]},
        {
            "version": "checks-v2",
            "names": ["Tests (default)", "Tests (postgres-integration)", "Tests (lint)"],
        },
        {"version": "checks-v1", "names": ["Tests (postgres-integration)", "Tests (default)"]},
    ],
    ids=["weaker", "stronger", "reordered"],
)
async def test_subject_check_set_is_informational_and_the_snapshot_decides(
    attestation_db, tmp_path, tree_checks
):
    await _reset_candidate_for_observation(attestation_db)
    client = ProviderClient()
    client.workflow_offset = 1000
    asked = _recording(client)
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document(required_checks=tree_checks)),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )

    result = await service.handle_candidate_ci(dict(CANDIDATE_ROW), 10.0)

    assert result["outcome"] == "published"
    # Observation and publication each read one listing; the snapshot's names
    # select from it (asserted on the published checks below).
    assert len(asked) == 2
    published = json.loads(client.records[0]["output"]["text"])
    assert published["required_check_set_version"] == "checks-v1"
    assert tuple(check["name"] for check in published["checks"]) == SNAPSHOT_CHECKS
    assert await service.subject_trust_blockers("p") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_kind", "expected_outcome", "expected_event"),
    [
        ("missing", "red", "integration.candidate_red"),
        ("failure", "red", "integration.candidate_red"),
    ],
)
async def test_subject_check_set_cannot_lower_the_snapshot_bar(
    attestation_db, tmp_path, provider_kind, expected_outcome, expected_event
):
    """A tree that drops a required check from its manifest still owes it."""
    await _reset_candidate_for_observation(attestation_db)
    client = ProviderClient()
    client.workflow_offset = 1000
    original = client.paged_items

    async def dropped(path, *, key):
        rows = await original(path, key=key)
        if key == "check_runs" and "check_name=" not in path:
            postgres = "Tests (postgres-integration)"
            if provider_kind == "missing":
                return [row for row in rows if row["name"] != postgres]
            rows = [
                {**row, "conclusion": "failure"} if row["name"] == postgres else row
                for row in rows
            ]
        return rows

    client.paged_items = dropped
    weakened = {"version": "checks-v0", "names": ["Tests (default)"]}
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document(required_checks=weakened)),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )

    result = await service.handle_candidate_ci(dict(CANDIDATE_ROW), 10.0)

    assert result["outcome"] == expected_outcome
    assert client.published == 0
    async with attestation_db._engine.connect() as conn:
        events = (await conn.execute(select(integration_outbox))).mappings().all()
        evidence = (
            await conn.execute(
                select(integration_check_evidence).where(
                    integration_check_evidence.c.id != "ci-aggregate"
                )
            )
        ).mappings().all()
    assert [event["event_type"] for event in events] == (
        [expected_event] if expected_event else []
    )
    assert all(row["required_check_version"] == "checks-v1" for row in evidence)
    if provider_kind == "failure":
        assert {name for row in evidence for name in row["checks"]} == set(SNAPSHOT_CHECKS)
    else:
        assert any(
            row["checks"].get("Tests (postgres-integration)") == "missing"
            for row in evidence
        )




@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["subject_moved", "operation_ended"])
async def test_subject_trust_report_retires_with_its_subject(attestation_db, tmp_path, change):
    await _reset_candidate_for_observation(attestation_db)
    client = ProviderClient()
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(None),
        github_client_factory=lambda binding: client,
    )
    await service.handle_candidate_ci(dict(CANDIDATE_ROW), 10.0)
    assert [blocker["cause"] for blocker in await service.subject_trust_blockers("p")] == [
        "missing"
    ]

    async with attestation_db.immediate() as conn:
        if change == "subject_moved":
            await conn.execute(
                update(integration_candidate_revisions)
                .where(integration_candidate_revisions.c.batch_id == "batch")
                .values(head_sha="b" * 40)
            )
        else:
            await conn.execute(
                update(integration_repair_operations)
                .where(integration_repair_operations.c.id == "root-op")
                .values(state="completed")
            )

    assert await service.subject_trust_blockers("p") == []
    assert service._subject_trust == {}


@pytest.mark.asyncio
async def test_parent_and_root_boundaries_take_their_own_checks_from_one_tree(tmp_path):
    """App mode no longer forces the parent and root check sets to be equal."""
    client = ProviderClient()
    service = IntegrationAttestationService(
        None,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
    )
    root = {
        "version": "checks-v1",
        "names": list(SNAPSHOT_CHECKS),
        "producer_id": "404",
    }
    parent = {"version": "focused-v1", "names": ["Tests (default)"], "producer_id": "404"}
    state = {
        "project_id": "p",
        "operation_id": "op",
        "canonical_repository_id": "repo-config-1",
        "repository_numeric_id": 303,
        "repository_full_name": "acme/widgets",
        "policy_snapshot": {
            "root": {"required_checks": root},
            "parent": {"required_checks": parent},
        },
        "batch_id": "op",
        "revision": 1,
        "parent_task_id": "parent",
        "generation": 1,
        "candidate_sha": SHA,
    }

    root_trust, _ = await service._load_trust(dict(state))
    parent_trust, _ = await service._load_trust(dict(state), boundary="parent")

    assert isinstance(root_trust, IntegrationTrustManifest)
    assert (root_trust.required_checks.version, root_trust.required_checks.names) == (
        "checks-v1",
        SNAPSHOT_CHECKS,
    )
    assert (parent_trust.required_checks.version, parent_trust.required_checks.names) == (
        "focused-v1",
        ("Tests (default)",),
    )
    assert root_trust.model_copy(update={"required_checks": parent_trust.required_checks}) == (
        parent_trust
    )


@pytest.mark.asyncio
async def test_subject_producer_is_compared_with_the_snapshot_boundary(tmp_path):
    client = ProviderClient()
    service = IntegrationAttestationService(
        None,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
    )
    required = {"version": "checks-v1", "names": list(SNAPSHOT_CHECKS), "producer_id": "15368"}
    state = {
        "canonical_repository_id": "repo-config-1",
        "repository_numeric_id": 303,
        "repository_full_name": "acme/widgets",
        "policy_snapshot": {"root": {"required_checks": required}},
        "batch_id": "batch",
        "revision": 0,
        "candidate_sha": SHA,
    }

    with pytest.raises(SubjectTrustError) as refused:
        await service._load_trust(state)

    assert (refused.value.cause, refused.value.fields) == (
        "identity_mismatch",
        ("ci_producer_app_id",),
    )


@pytest.mark.asyncio
async def test_parent_boundary_batch_subject_is_refused_as_its_batch(tmp_path):
    """The train reads an epic head's parent checks under a batch identity, not a parent's."""
    service = IntegrationAttestationService(
        None,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(None),
        github_client_factory=lambda binding: ProviderClient(),
    )
    required = {"version": "checks-v1", "names": list(SNAPSHOT_CHECKS), "producer_id": "404"}
    state = {
        "project_id": "p",
        "operation_id": "epic-readiness-epic",
        "canonical_repository_id": "repo-config-1",
        "repository_numeric_id": 303,
        "repository_full_name": "acme/widgets",
        "policy_snapshot": {"parent": {"required_checks": required}},
        "batch_id": "epic-readiness-epic",
        "revision": 0,
        "candidate_sha": SHA,
    }

    with pytest.raises(SubjectTrustError) as refused:
        await service._load_trust(state, boundary="parent")

    assert refused.value.cause == "missing"
    failure = service._subject_trust["epic-readiness-epic"]
    assert (failure["target_kind"], failure["subject"], failure["head_sha"]) == (
        "batch",
        {"batch_id": "epic-readiness-epic", "revision": 0},
        SHA,
    )


@pytest.mark.asyncio
async def test_slug_producer_in_app_mode_is_a_policy_fault_not_a_subject_refusal(tmp_path):
    client = ProviderClient()
    service = IntegrationAttestationService(
        None,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
    )
    required = {
        "version": "checks-v1",
        "names": list(SNAPSHOT_CHECKS),
        "producer_id": "github-actions",
    }
    state = {
        "project_id": "p",
        "operation_id": "root-op",
        "canonical_repository_id": "repo-config-1",
        "repository_numeric_id": 303,
        "repository_full_name": "acme/widgets",
        "policy_snapshot": {"root": {"required_checks": required}},
        "batch_id": "batch",
        "revision": 0,
        "candidate_sha": SHA,
    }

    with pytest.raises(AttestationError, match="numeric") as refused:
        await service._load_trust(state)

    assert not isinstance(refused.value, SubjectTrustError)
    assert service._subject_trust == {}


@pytest.mark.asyncio
async def test_publish_revalidates_subject_after_provider_io(attestation_db, tmp_path):
    client = ProviderClient()

    async def change_current_subject(_binding):
        return client

    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=change_current_subject,
    )
    original = client.request_json

    async def publish_then_move(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with attestation_db.immediate() as conn:
            await conn.execute(
                update(integration_candidate_revisions).values(state="superseded")
            )
        return result

    client.request_json = publish_then_move

    result = await service.publish(subject())

    assert result.outcome == "stale"
    assert result.proof is None


@pytest.mark.asyncio
async def test_publish_crash_replays_existing_record_without_duplicate(attestation_db, tmp_path):
    client = ProviderClient()
    now = [10.0]

    async def crash(phase):
        if phase == "after_attestation_publication":
            raise RuntimeError("simulated daemon loss")

    first = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        crash_hook=crash,
        clock=lambda: now[0],
    )
    with pytest.raises(RuntimeError, match="simulated daemon loss"):
        await first.publish(subject())

    restarted = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )
    assert (await restarted.publish(subject())).outcome == "configuration_blocked"
    now[0] = 311.0
    replay = await restarted.publish(subject())

    assert replay.outcome == "already_published"
    assert replay.proof is not None
    assert await restarted.resolve(subject()) == replay.proof
    assert client.published == 1


@pytest.mark.asyncio
async def test_lost_publication_response_reconciles_without_duplicate(attestation_db, tmp_path):
    client = ProviderClient()
    now = [10.0]
    original = client.request_json

    async def publish_then_lose_response(*args, **kwargs):
        await original(*args, **kwargs)
        raise GitHubAppError("transient", "response lost")

    client.request_json = publish_then_lose_response
    first = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )
    assert (await first.publish(subject())).outcome == "configuration_blocked"

    client.request_json = original
    restarted = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )
    assert (await restarted.publish(subject())).outcome == "configuration_blocked"
    now[0] = 311.0
    result = await restarted.publish(subject())
    assert result.outcome == "already_published"
    assert result.proof is not None
    assert client.published == 1


@pytest.mark.asyncio
async def test_marked_publication_freezes_execution_nonce(attestation_db, tmp_path):
    client = ProviderClient()

    async def lose_response(*args, **kwargs):
        await ProviderClient.request_json(client, *args, **kwargs)
        raise GitHubAppError("transient", "response lost")

    client.request_json = lose_response
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: 10.0,
    )
    assert (await service.publish(subject())).outcome == "configuration_blocked"

    with pytest.raises(DBAPIError, match="immutable"):
        async with attestation_db.immediate() as conn:
            await conn.execute(
                update(integration_attestation_publications).values(
                    execution_nonce="replacement-nonce"
                )
            )


@pytest.mark.asyncio
async def test_newest_invalid_trusted_record_blocks_older_success(attestation_db, tmp_path):
    client = ProviderClient()
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
    )
    assert (await service.publish(subject())).outcome == "published"
    invalid = dict(client.records[0])
    invalid.update(id=7002, conclusion="neutral")
    client.records.append(invalid)

    assert (await service.publish(subject())).outcome == "configuration_blocked"
    assert await service.resolve(subject()) is None
    assert client.published == 1


@pytest.mark.asyncio
async def test_enablement_projection_is_read_only_and_fail_closed(attestation_db, tmp_path):
    calls = []

    async def reader(repository_id):
        calls.append(repository_id)
        return True

    ready = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: ProviderClient(),
        protection_reader=reader,
        probe_reader=reader,
        debug_class_reader=reader,
    )
    blocked = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=None,
    )

    assert (await ready.enablement_blockers("repo-config-1")).ready is True
    result = await blocked.enablement_blockers("repo-config-1")
    assert result.ready is False
    assert result.blockers == (
        "missing_trusted_integration_app",
        "debug_intelligence_class_unresolved",
        "branch_protection_incompatible",
        "scratch_probe_missing_or_failed",
    )
    assert calls == ["repo-config-1"] * 3

    async def unavailable(_repository_id):
        raise OSError("local read failed")

    failed = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: ProviderClient(),
        protection_reader=unavailable,
        probe_reader=unavailable,
        debug_class_reader=unavailable,
    )
    failure = await failed.enablement_blockers("repo-config-1")
    assert failure.ready is False
    assert failure.blockers == (
        "debug_intelligence_class_unresolved",
        "branch_protection_incompatible",
        "scratch_probe_missing_or_failed",
    )


@pytest.mark.asyncio
async def test_green_candidate_remains_retryable_until_main_promotion(attestation_db):
    rows = await attestation_db.pending_candidate_ci_page(after=None, limit=10)
    assert [(row["batch_id"], row["revision"]) for row in rows] == [("batch", 0)]

    async with attestation_db.immediate() as conn:
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="promoting")
        )

    assert await attestation_db.pending_candidate_ci_page(after=None, limit=10) == []


@pytest.mark.asyncio
async def test_two_fresh_services_reserve_one_provider_publication(attestation_db, tmp_path):
    class RacingProvider(ProviderClient):
        def __init__(self):
            super().__init__()
            self.entered = 0
            self.both_entered = asyncio.Event()

        async def request_json(self, method, path, *, json_body, expected_statuses):
            self.entered += 1
            if self.entered == 2:
                self.both_entered.set()
            try:
                await asyncio.wait_for(self.both_entered.wait(), timeout=0.5)
            except TimeoutError:
                pass
            return await super().request_json(
                method, path, json_body=json_body, expected_statuses=expected_statuses
            )

    client = RacingProvider()

    def fresh():
        return IntegrationAttestationService(
            attestation_db,
            data_dir=tmp_path,
            git_manager=ExactTreeGit(trust_document()),
            github_client_factory=lambda binding: client,
            clock=lambda: 10.0,
        )

    results = await asyncio.gather(fresh().publish(subject()), fresh().publish(subject()))

    assert client.published == 1
    assert sum(result.proof is not None for result in results) == 1
    assert {result.outcome for result in results} <= {
        "published",
        "already_published",
        "configuration_blocked",
    }


async def _wait_until_publication_phase(phase: asyncio.Event, publication: asyncio.Task) -> None:
    """Wait for setup without treating a busy CI worker as a publication hang."""
    waiter = asyncio.create_task(phase.wait())
    try:
        await asyncio.wait({waiter, publication}, return_when=asyncio.FIRST_COMPLETED)
        if not phase.is_set():
            pytest.fail(f"publication ended before the expected phase: {publication.result()!r}")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_unmarked_takeover_fences_paused_old_finalizer(
    attestation_db, tmp_path
):
    now = [10.0]
    old_ready = asyncio.Event()
    release_old = asyncio.Event()
    successor_prewrite = asyncio.Event()
    release_successor = asyncio.Event()
    payload = attestation_payload()
    old_client = ProviderClient()
    old_client.records.append(
        {
            "id": 7000,
            "name": ATTESTATION_CHECK_NAME,
            "app": {"id": 101},
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
            "external_id": payload.external_id,
            "output": {"text": payload.canonical_bytes().decode("ascii")},
        }
    )
    old = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: old_client,
        clock=lambda: now[0],
    )
    finish = old._finish_publication

    async def pause_old_finalizer(*args, **kwargs):
        old_ready.set()
        await release_old.wait()
        return await finish(*args, **kwargs)

    old._finish_publication = pause_old_finalizer
    old_task = asyncio.create_task(old.publish(subject()))
    await _wait_until_publication_phase(old_ready, old_task)

    now[0] = 311.0
    successor_client = ProviderClient()
    post = successor_client.request_json

    async def pause_successor_post(*args, **kwargs):
        successor_prewrite.set()
        await release_successor.wait()
        return await post(*args, **kwargs)

    successor_client.request_json = pause_successor_post
    successor = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: successor_client,
        clock=lambda: now[0],
    )
    successor_task = asyncio.create_task(successor.publish(subject()))
    await _wait_until_publication_phase(successor_prewrite, successor_task)

    release_old.set()
    old_result = await asyncio.wait_for(old_task, timeout=1.0)
    release_successor.set()
    successor_result = await asyncio.wait_for(successor_task, timeout=1.0)

    assert old_result.outcome == "stale"
    assert old_result.proof is None
    assert successor_result.outcome == "published"
    assert successor_result.proof is not None
    assert successor_result.proof.check_run_id == 7001
    assert successor_client.published == 1
    async with attestation_db._engine.connect() as conn:
        canonical = (
            await conn.execute(select(integration_attestation_publications))
        ).mappings().one()
    assert canonical["state"] == "published"
    assert canonical["check_run_id"] == 7001


@pytest.mark.asyncio
async def test_expired_unmarked_reservation_can_be_taken_over(attestation_db, tmp_path):
    now = [10.0]
    client = ProviderClient()

    async def crash(phase):
        if phase == "after_publication_reservation":
            raise RuntimeError("lost before provider write")

    first = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        crash_hook=crash,
        clock=lambda: now[0],
    )
    with pytest.raises(RuntimeError, match="lost before provider write"):
        await first.publish(subject())
    now[0] = 311.0
    successor = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )

    result = await successor.publish(subject())
    assert result.outcome == "published"
    assert client.published == 1


@pytest.mark.asyncio
async def test_expired_marked_reservation_reconciles_but_never_reposts(attestation_db, tmp_path):
    now = [10.0]

    class LostBeforeProviderRecord(ProviderClient):
        def __init__(self):
            super().__init__()
            self.posts = 0

        async def request_json(self, method, path, *, json_body, expected_statuses):
            self.posts += 1
            raise GitHubAppError("transient", "ambiguous provider response")

    client = LostBeforeProviderRecord()
    first = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )
    assert (await first.publish(subject())).outcome == "configuration_blocked"
    now[0] = 311.0
    successor = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )

    assert (await successor.publish(subject())).outcome == "configuration_blocked"
    assert client.posts == 1
    async with attestation_db._engine.connect() as conn:
        claim = (
            await conn.execute(select(integration_attestation_publications))
        ).mappings().one()
    assert claim["state"] == "reserved" and claim["prewrite_at"] == 10.0


@pytest.mark.asyncio
async def test_live_publication_reservation_blocks_stage_expiry(attestation_db, tmp_path):
    reserved = asyncio.Event()
    release = asyncio.Event()

    async def pause_after_reservation(phase):
        if phase == "after_publication_reservation":
            reserved.set()
            await release.wait()

    publisher = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        github_client_factory=lambda binding: ProviderClient(),
        clock=lambda: 900.0,
        crash_hook=pause_after_reservation,
    )
    task = asyncio.create_task(publisher.publish(subject()))
    try:
        # Bound a deadlock across the scenario, not PostgreSQL setup latency.
        async with asyncio.timeout(60):
            await _wait_until_publication_phase(reserved, task)

            result = await RepairService(attestation_db, clock=lambda: 1001.0).expire(
                "root-op", 0, now=1001.0
            )

            assert result["outcome"] == "not_due"
            assert result["action"] == "wait"
            release.set()
            assert (await task).outcome == "published"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives
    authorize_root_primitives(monkeypatch)
