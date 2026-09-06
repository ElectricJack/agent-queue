from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError

from src.database import Database
from src.database.tables import (
    integration_batches,
    integration_attestation_publications,
    integration_candidate_publications,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_repair_operations,
    integration_repair_stages,
)
from src.git.github_app import GitHubAppError, GitHubRepositoryBinding
from src.integration.attestation import IntegrationAttestationService
from src.integration.ci import ATTESTATION_CHECK_NAME, AttestationPayload
from src.integration.main_promotion import RootAttestationSubject
from src.integration.repair import RepairService
from src.models import Project, RepoConfig, RepoSourceType


SHA = "a" * 40
BASE = "0" * 40


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
async def attestation_db(tmp_path):
    db = Database(str(tmp_path / "attestation.db"))
    await db.initialize()
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
    await db.close()


class ExactTreeGit:
    def __init__(self, manifest: bytes):
        self.manifest = manifest
        self.fetches: list[dict] = []

    async def afetch_exact_oid_with_app_auth(self, destination_git_dir, **kwargs):
        self.fetches.append({"destination_git_dir": destination_git_dir, **kwargs})
        return kwargs["oid"]

    async def arun_git_result(self, args, **kwargs):
        assert args == ["show", f"{SHA}:.github/agent-queue-integration.json"]
        return SimpleNamespace(returncode=0, stdout=self.manifest.decode(), stderr="")


class ProviderClient:
    def __init__(self):
        self.config = SimpleNamespace(app_id=101)
        self.repository = GitHubRepositoryBinding(303, "acme/widgets")
        self.records: list[dict] = []
        self.published = 0

    async def installation_token(self):
        return "dummy-installation-token"

    async def paged_items(self, path, *, key):
        if key == "workflow_runs":
            return [
                {
                    "id": 31,
                    "workflow_id": 301,
                    "run_attempt": 2,
                    "check_suite_id": 21,
                    "head_sha": SHA,
                    "conclusion": "success",
                },
                {
                    "id": 32,
                    "workflow_id": 302,
                    "run_attempt": 1,
                    "check_suite_id": 22,
                    "head_sha": SHA,
                    "conclusion": "success",
                },
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


@pytest.mark.asyncio
async def test_publish_reads_trust_from_authenticated_exact_candidate_oid(attestation_db, tmp_path):
    git = ExactTreeGit(trust_document())
    client = ProviderClient()
    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=git,
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: client,
    )

    result = await service.publish(subject())

    assert result.outcome == "configuration_blocked"
    assert result.proof is None
    assert client.published == 0
    async with attestation_db._engine.connect() as conn:
        assert await conn.scalar(select(integration_candidate_revisions.c.state)) == "green"


@pytest.mark.asyncio
async def test_publish_revalidates_subject_after_provider_io(attestation_db, tmp_path):
    client = ProviderClient()

    async def change_current_subject(_binding):
        return client

    service = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        app_client_factory=change_current_subject,
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
        app_client_factory=lambda binding: client,
        crash_hook=crash,
        clock=lambda: now[0],
    )
    with pytest.raises(RuntimeError, match="simulated daemon loss"):
        await first.publish(subject())

    restarted = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )
    assert (await first.publish(subject())).outcome == "configuration_blocked"

    client.request_json = original
    restarted = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: ProviderClient(),
        protection_reader=reader,
        probe_reader=reader,
        debug_class_reader=reader,
    )
    blocked = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        app_client_factory=None,
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
        app_client_factory=lambda binding: ProviderClient(),
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
            app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: old_client,
        clock=lambda: now[0],
    )
    finish = old._finish_publication

    async def pause_old_finalizer(*args, **kwargs):
        old_ready.set()
        await release_old.wait()
        return await finish(*args, **kwargs)

    old._finish_publication = pause_old_finalizer
    old_task = asyncio.create_task(old.publish(subject()))
    await asyncio.wait_for(old_ready.wait(), timeout=1.0)

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
        app_client_factory=lambda binding: successor_client,
        clock=lambda: now[0],
    )
    successor_task = asyncio.create_task(successor.publish(subject()))
    await asyncio.wait_for(successor_prewrite.wait(), timeout=1.0)

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
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: client,
        clock=lambda: now[0],
    )
    assert (await first.publish(subject())).outcome == "configuration_blocked"
    now[0] = 311.0
    successor = IntegrationAttestationService(
        attestation_db,
        data_dir=tmp_path,
        git_manager=ExactTreeGit(trust_document()),
        app_client_factory=lambda binding: client,
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
        app_client_factory=lambda binding: ProviderClient(),
        clock=lambda: 900.0,
        crash_hook=pause_after_reservation,
    )
    task = asyncio.create_task(publisher.publish(subject()))
    await asyncio.wait_for(reserved.wait(), timeout=1.0)

    result = await RepairService(attestation_db, clock=lambda: 1001.0).expire(
        "root-op", 0, now=1001.0
    )

    assert result["outcome"] == "not_due"
    assert result["action"] == "wait"
    release.set()
    assert (await asyncio.wait_for(task, timeout=1.0)).outcome == "published"
