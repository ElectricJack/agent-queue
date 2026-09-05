from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_parent_episodes,
    integration_repair_operations,
    task_integration_checkpoints,
)

from src.integration.ci import (
    ATTESTATION_CHECK_NAME,
    AttestationError,
    AttestationPayload,
    IntegrationTrustManifest,
    AuthenticatedGitHubObserver,
    CandidateCISubject,
    CIService,
    FailedCIObservation,
    ParentCISubject,
    TrustedCIObservation,
    TrustedFixtureObserver,
    select_trusted_attestation,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus


SHA = "a" * 40


def policy_snapshot() -> dict:
    required = {"version": "checks-v1", "names": ["unit", "postgres"], "producer_id": "404"}
    return {"parent": {"required_checks": required}, "root": {"required_checks": required}}


def trust() -> IntegrationTrustManifest:
    return IntegrationTrustManifest.model_validate(
        {
            "schema": "aq.integration-trust.v1",
            "canonical_repository_id": "repo-config-1",
            "repository_id": 303,
            "full_name": "acme/widgets",
            "ci_producer_app_id": 404,
            "attestation_app_id": 101,
            "attestation_name": ATTESTATION_CHECK_NAME,
            "required_checks": {"version": "checks-v1", "names": ["unit", "postgres"]},
        }
    )


def payload_dict() -> dict:
    return {
        "schema": "aq.integration-attestation.v1",
        "canonical_repository_id": "repo-config-1",
        "repository_id": 303,
        "ci_producer_app_id": 404,
        "attestation_app_id": 101,
        "head_sha": SHA,
        "required_check_set_version": "checks-v1",
        "checks": [
            {
                "name": "unit", "check_run_id": 11, "check_suite_id": 21,
                "producer_app_id": 404, "head_sha": SHA, "conclusion": "success",
            },
            {
                "name": "postgres", "check_run_id": 12, "check_suite_id": 22,
                "producer_app_id": 404, "head_sha": SHA, "conclusion": "success",
            },
        ],
        "workflow_runs": [
            {
                "workflow_run_id": 31, "run_attempt": 2, "check_suite_id": 21,
                "head_sha": SHA, "conclusion": "success",
            },
            {
                "workflow_run_id": 32, "run_attempt": 1, "check_suite_id": 22,
                "head_sha": SHA, "conclusion": "success",
            },
        ],
    }


def canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def check_record(record_id: int, payload: bytes, *, conclusion: str = "success") -> dict:
    parsed = AttestationPayload.from_canonical_bytes(payload)
    return {
        "id": record_id,
        "name": ATTESTATION_CHECK_NAME,
        "app": {"id": 101},
        "head_sha": SHA,
        "status": "completed",
        "conclusion": conclusion,
        "external_id": parsed.external_id,
        "output": {"text": payload.decode()},
    }


def test_manifest_rejects_bool_ids_duplicate_checks_and_equal_apps():
    base = trust().model_dump(mode="json", by_alias=True)
    for mutate in (
        lambda value: value.update(repository_id=True),
        lambda value: value["required_checks"].update(names=["unit", "unit"]),
        lambda value: value.update(attestation_app_id=404),
    ):
        changed = json.loads(json.dumps(base))
        mutate(changed)
        with pytest.raises(ValidationError):
            IntegrationTrustManifest.model_validate(changed)


def test_canonical_attestation_rejects_noncanonical_bytes_and_mixed_suite_attempt():
    data = payload_dict()
    noncanonical = json.dumps(data, indent=2).encode()
    with pytest.raises(AttestationError, match="noncanonical"):
        AttestationPayload.from_canonical_bytes(noncanonical)

    data["workflow_runs"][1]["check_suite_id"] = 21
    with pytest.raises((AttestationError, ValidationError)):
        AttestationPayload.from_canonical_bytes(canonical(data))


def test_newest_exact_name_trusted_app_record_wins_and_invalid_newest_fails_closed():
    payload = canonical(payload_dict())
    older = check_record(50, payload)
    newest = check_record(90, payload, conclusion="failure")
    ignored_other_app = check_record(100, payload)
    ignored_other_app["app"] = {"id": 999}

    with pytest.raises(AttestationError, match="newest"):
        select_trusted_attestation(
            [older, newest, ignored_other_app], trust(), expected_head_sha=SHA
        )

    selected = select_trusted_attestation([older], trust(), expected_head_sha=SHA)
    assert selected.external_id.startswith("aq-attestation-v1:")
    assert selected.checks[0].name == "unit"


class FakeGitHubClient:
    def __init__(self, checks, workflows):
        self.checks = checks
        self.workflows = workflows
        self.paths = []
        self.published = []

    async def paged_items(self, path, *, key, max_pages=20):
        self.paths.append((path, key))
        if key == "check_runs":
            if "Agent%20Queue%20Integration%20Attestation" in path:
                return self.checks.get("attestation", [])
            name = "unit" if "unit" in path else "postgres"
            return self.checks.get(name, [])
        return self.workflows

    async def request_json(self, method, path, *, json_body=None, expected_statuses=None):
        self.published.append((method, path, json_body, expected_statuses))
        return {"id": 77}


@pytest.mark.asyncio
async def test_authenticated_observer_selects_newest_exact_producer_and_coherent_attempts():
    checks = {
        "unit": [
            {"id": 9, "name": "unit", "head_sha": SHA, "status": "completed",
             "conclusion": "success", "app": {"id": 999}, "check_suite": {"id": 21}},
            {"id": 11, "name": "unit", "head_sha": SHA, "status": "completed",
             "conclusion": "success", "app": {"id": 404}, "check_suite": {"id": 21}},
        ],
        "postgres": [
            {"id": 12, "name": "postgres", "head_sha": SHA, "status": "completed",
             "conclusion": "success", "app": {"id": 404}, "check_suite": {"id": 22}}
        ],
    }
    workflows = [
        {"id": 31, "workflow_id": 301, "run_attempt": 2, "check_suite_id": 21,
         "head_sha": SHA, "conclusion": "success"},
        {"id": 32, "workflow_id": 302, "run_attempt": 1, "check_suite_id": 22,
         "head_sha": SHA, "conclusion": "success"},
    ]
    client = FakeGitHubClient(checks, workflows)

    observation = await AuthenticatedGitHubObserver(client).observe(trust(), SHA)

    assert observation.payload == AttestationPayload.model_validate(payload_dict())
    assert observation.workflow_ids == {21: 301, 22: 302}
    assert all("filter=all" in path for path, key in client.paths if key == "check_runs")


@pytest.mark.asyncio
async def test_authenticated_observer_rejects_partial_required_matrix():
    client = FakeGitHubClient({"unit": [], "postgres": []}, [])
    with pytest.raises(AttestationError, match="missing"):
        await AuthenticatedGitHubObserver(client).observe(trust(), SHA)


@pytest.mark.asyncio
async def test_authenticated_observer_returns_normalized_conclusive_failure():
    checks = {
        "unit": [{"id": 11, "name": "unit", "head_sha": SHA, "status": "completed",
                  "conclusion": "failure", "app": {"id": 404},
                  "check_suite": {"id": 21}}],
        "postgres": [{"id": 12, "name": "postgres", "head_sha": SHA,
                      "status": "completed", "conclusion": "success", "app": {"id": 404},
                      "check_suite": {"id": 21}}],
    }
    workflows = [{"id": 31, "workflow_id": 301, "run_attempt": 2, "check_suite_id": 21,
                  "head_sha": SHA, "conclusion": "failure"}]

    observation = await AuthenticatedGitHubObserver(
        FakeGitHubClient(checks, workflows)
    ).observe(trust(), SHA)

    assert isinstance(observation, FailedCIObservation)
    assert observation.conclusion == "failure"


@pytest.mark.asyncio
async def test_publish_attestation_uses_canonical_text_and_digest():
    client = FakeGitHubClient({}, [])
    observer = AuthenticatedGitHubObserver(client)
    payload = AttestationPayload.model_validate(payload_dict())

    result = await observer.publish(trust(), payload)

    assert result == 77
    body = client.published[0][2]
    assert body["name"] == ATTESTATION_CHECK_NAME
    assert body["external_id"] == payload.external_id
    assert body["output"]["text"].encode() == payload.canonical_bytes()


@pytest.mark.asyncio
async def test_publish_attestation_reuses_byte_identical_trusted_record():
    payload = AttestationPayload.model_validate(payload_dict())
    existing = check_record(71, payload.canonical_bytes())
    client = FakeGitHubClient({"attestation": [existing]}, [])

    result = await AuthenticatedGitHubObserver(client).publish(trust(), payload)

    assert result == 71
    assert client.published == []


@pytest.fixture
async def ci_db(tmp_path):
    database = Database(str(tmp_path / "ci.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="project"))
    await database.create_repo(
        RepoConfig(id="repo-config-1", project_id="p", source_type=RepoSourceType.LINK)
    )
    await database.create_task(
        Task(
            id="parent", project_id="p", repo_id="repo-config-1", branch_name="aq/parent",
            title="parent", description="parent", status=TaskStatus.IN_PROGRESS,
        )
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode", parent_task_id="parent", repository_id="repo-config-1",
                generation=3, pre_collection_checkpoint_sha="0" * 40, created_at=1.0,
            )
        )
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent", repository_id="repo-config-1", branch="aq/parent",
                episode_id="episode", generation=3, checkpoint_sha=SHA,
                state="verifying", version=1, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="parent-op", target_kind="parent", parent_task_id="parent",
                episode_id="episode", state="active", active_stage=0,
                policy_snapshot=policy_snapshot(), artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0, updated_at=1.0,
            )
        )
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_ci_service_persists_normalized_parent_evidence_for_task6_consumer(ci_db):
    observation = TrustedCIObservation(
        payload=AttestationPayload.model_validate(payload_dict()),
        workflow_ids={21: 301, 22: 302},
    )
    timestamps = iter((9.0, 9.0, 10.0, 10.0))
    service = CIService(
        ci_db, trust(), TrustedFixtureObserver(observation), clock=lambda: next(timestamps)
    )

    result = await service.observe_parent(
        ParentCISubject(
            operation_id="parent-op", parent_task_id="parent", generation=3, head_sha=SHA
        )
    )
    replay = await service.observe_parent(
        ParentCISubject(
            operation_id="parent-op", parent_task_id="parent", generation=3, head_sha=SHA
        )
    )

    assert result["outcome"] == "green"
    assert replay == result
    async with ci_db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_check_evidence))).mappings().all()
    assert {name for row in rows for name in row["checks"]} == {"unit", "postgres"}
    assert all(
        row["operation_id"] == "parent-op"
        and row["parent_generation"] == 3
        and row["parent_head_sha"] == SHA
        and row["producer_id"] == "404"
        for row in rows
    )


@pytest.mark.asyncio
async def test_ci_service_rejects_stale_parent_and_untyped_caller_evidence(ci_db):
    observation = TrustedCIObservation(
        payload=AttestationPayload.model_validate(payload_dict()), workflow_ids={21: 301, 22: 302}
    )
    service = CIService(ci_db, trust(), TrustedFixtureObserver(observation))

    assert (
        await service.observe_parent(
            ParentCISubject(
                operation_id="parent-op", parent_task_id="parent", generation=2, head_sha=SHA
            )
        )
    )["outcome"] == "stale_subject"
    with pytest.raises(TypeError):
        await service.observe_parent({"success": True, "tests": "passed"})


@pytest.mark.asyncio
async def test_ci_service_appends_conclusive_failure_without_marking_green(ci_db):
    failure = FailedCIObservation(
        checks=(
            {"name": "unit", "check_run_id": 11, "check_suite_id": 21,
             "producer_app_id": 404, "head_sha": SHA, "conclusion": "failure"},
            {"name": "postgres", "check_run_id": 12, "check_suite_id": 21,
             "producer_app_id": 404, "head_sha": SHA, "conclusion": "success"},
        ),
        workflow_runs=({"workflow_run_id": 31, "run_attempt": 2, "check_suite_id": 21,
                        "head_sha": SHA, "conclusion": "failure"},),
        workflow_ids={21: 301},
        conclusion="failure",
    )
    service = CIService(ci_db, trust(), TrustedFixtureObserver(failure), clock=lambda: 9.0)

    result = await service.observe_parent(
        ParentCISubject(
            operation_id="parent-op", parent_task_id="parent", generation=3, head_sha=SHA
        )
    )

    assert result["outcome"] == "red"
    async with ci_db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_check_evidence))).mappings().all()
    assert len(rows) == 1
    assert rows[0]["conclusion"] == "failure"


@pytest.mark.asyncio
async def test_parent_declared_check_transport_absence_requests_safe_full_suite(ci_db):
    declared_policy = policy_snapshot()
    declared_policy["parent"] = {
        "required_checks": {
            "version": "focused-v1", "names": ["focused"], "producer_id": "404"
        }
    }
    async with ci_db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "parent-op")
            .values(policy_snapshot=declared_policy, required_check_version="focused-v1")
        )
    root_observation = TrustedCIObservation(
        payload=AttestationPayload.model_validate(payload_dict()), workflow_ids={21: 301, 22: 302}
    )
    service = CIService(ci_db, trust(), TrustedFixtureObserver(root_observation))

    result = await service.observe_parent(
        ParentCISubject(
            operation_id="parent-op", parent_task_id="parent", generation=3, head_sha=SHA
        )
    )

    assert result == {"outcome": "full_suite_required", "evidence_ids": []}


@pytest.mark.asyncio
async def test_ci_service_binds_root_evidence_to_exact_batch_revision_and_candidate(ci_db):
    async with ci_db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch", project_id="p", repository_id="repo-config-1", request_id="request",
                source_manifest_digest="sha256:" + "d" * 64, base_sha="0" * 40,
                lifecycle="testing", current_revision=4, integration_branch="integration/batch",
                policy_snapshot=policy_snapshot(), artifact_snapshot={}, cleanup_state="pending",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="batch", revision=4, construction_base_sha="0" * 40,
                next_member_ordinal=2, head_sha=SHA, state="testing", created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="root-op", target_kind="batch", batch_id="batch", episode_id="batch",
                state="active", active_stage=0, policy_snapshot=policy_snapshot(), artifact_snapshot={},
                required_check_version="checks-v1", created_at=1.0, updated_at=1.0,
            )
        )
    observation = TrustedCIObservation(
        payload=AttestationPayload.model_validate(payload_dict()), workflow_ids={21: 301, 22: 302}
    )
    service = CIService(ci_db, trust(), TrustedFixtureObserver(observation), clock=lambda: 9.0)

    result = await service.observe_candidate(
        CandidateCISubject(
            operation_id="root-op", batch_id="batch", revision=4, candidate_sha=SHA
        )
    )
    stale = await service.observe_candidate(
        CandidateCISubject(
            operation_id="root-op", batch_id="batch", revision=3, candidate_sha=SHA
        )
    )

    assert result["outcome"] == "green"
    assert stale["outcome"] == "stale_subject"
    async with ci_db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_check_evidence))).mappings().all()
    assert all(
        row["operation_id"] == "root-op"
        and row["batch_id"] == "batch"
        and row["candidate_revision"] == 4
        and row["parent_task_id"] is None
        for row in rows
    )
