"""Operational rollout controls for hierarchical integration."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import insert, select, update

import pytest

from src.database import Database
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    gates,
    integration_batches,
    integration_candidate_publications,
    integration_candidate_revisions,
    integration_cleanup_items,
    integration_legacy_gate_applicability,
    integration_repair_operations,
    integration_repair_stages,
    integration_rollout_transitions,
    project_integration_schedules,
    projects,
)
from src.config import GitHubAppConfig
from src.git.github_app import GitHubAppClient, GitHubRepositoryBinding, HttpResponse
from src.integration.controls import IntegrationControlService, daemon_functional_preflight
from src.integration.models import (
    ArtifactSnapshot,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.integration.scheduler import IntegrationScheduler
from src.integration.status import IntegrationStatusService
from src.models import Project, RepoConfig, RepoSourceType
from src.playbooks.artifact_ref import ArtifactRef
from src.profiles.capabilities import CapabilityPolicy


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "1" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        compiler_build="task11c-test",
        version=1,
    )


def _policy(*, branchless_parent: str = "verifier") -> dict:
    boundary = IntegrationBoundaryPolicy(
        required_checks=RequiredCheckSet(
            version="checks-v1", names=("Tests (default)",), producer_id="1234"
        ),
        repair=RepairPolicy(
            debug_intelligence_class="deep",
            debug_profile_id="debugger",
        ),
        route=PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            activation_id=None,
            artifact=_artifact(),
        ),
        primary_intelligence_class="standard",
        primary_profile_id="worker",
        verifier_intelligence_class=(
            "standard" if branchless_parent == "verifier" else None
        ),
        verifier_profile_id="verifier" if branchless_parent == "verifier" else None,
    )
    return HierarchicalIntegrationPolicy(
        parent=boundary,
        root=boundary,
        branchless_parent=branchless_parent,
        on_failed_child="block",
    ).model_dump(mode="json")


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "operational-controls.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    artifact = _artifact()
    await database.upsert_playbook_artifact(
        ArtifactRef(**artifact.model_dump(mode="json")),
        scope="project",
        scope_identifier="p",
        path="/artifacts/hierarchical-delivery.json",
        size_bytes=1,
    )
    await database.set_playbook_activation(
        playbook_id=artifact.playbook_id,
        scope="project",
        scope_identifier="p",
        artifact_sha256=artifact.artifact_sha256,
        enabled=True,
        activated_by="test",
        health="ready",
        reasons="[]",
    )
    await database.update_project(
        "p",
        integration_repository_id="repo",
        hierarchical_integration_policy=_policy(),
        integration_mode="pull_request",
    )
    yield database
    await database.close()


async def _external_ready(_project_id: str, _repository_id: str) -> tuple[str, ...]:
    return ()


async def _row_count(db, table) -> int:
    async with db._engine.connect() as conn:
        return len((await conn.execute(select(table))).all())


async def _schedule(db) -> dict:
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(project_integration_schedules))).mappings().one()
        return dict(row)


async def test_enable_is_atomic_and_stale_generation_changes_nothing(db):
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: 10.0)

    preflight = await service.preflight("p")
    assert preflight["ready"] is True
    assert preflight["blocker_digest"] == (
        "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    assert preflight["certification"] == {
        "status": "not_performed",
        "deferred": ("protection", "scratch_probe", "transport_isolation"),
    }

    enabled = await service.enable(
        "p",
        mode="train",
        expected_generation=0,
        reason="operator rollout",
        operator_id="operator:local",
    )
    assert enabled["outcome"] == "enabled"
    assert enabled["generation"] == 1
    project = await db.get_project("p")
    assert project.hierarchical_integration_mode == "train"
    assert project.hierarchical_integration_desired_mode == "train"
    assert project.hierarchical_integration_draining is False
    suppression = await db.get_integration_legacy_suppression("p")
    assert suppression["merge_sweep_suppressed"] is True
    assert suppression["final_review_route_suppressed"] is True
    assert suppression["legacy_gate_creation_suppressed"] is True

    stale = await service.enable(
        "p",
        mode="observe",
        expected_generation=0,
        reason="stale caller",
        operator_id="operator:local",
    )
    assert stale["outcome"] == "stale"
    assert await _row_count(db, integration_rollout_transitions) == 1
    assert (await db.get_project("p")).hierarchical_integration_mode == "train"


async def test_train_enable_configures_default_then_preserves_and_overrides_cadence(db):
    now = 10.0
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: now)

    await service.enable(
        "p",
        mode="train",
        expected_generation=0,
        reason="default cadence",
        operator_id="operator:local",
    )
    assert (await _schedule(db))["interval_seconds"] == 300

    now = 20.0
    await service.enable(
        "p",
        mode="train",
        expected_generation=1,
        reason="same mode preserves cadence",
        operator_id="operator:local",
    )
    preserved = await _schedule(db)
    assert preserved["interval_seconds"] == 300
    assert preserved["next_due_at"] == 310.0

    async with db.immediate() as conn:
        await conn.execute(
            update(project_integration_schedules)
            .where(project_integration_schedules.c.project_id == "p")
            .values(
                request_sequence=4,
                outstanding_request_id="integration-sweep:p:4",
                outstanding_trigger="manual",
                outstanding_requested_at=21.0,
                catchup_trigger="manual",
                catchup_requested_at=22.0,
                catchup_after_sequence=4,
            )
        )

    now = 50.0
    changed = await service.enable(
        "p",
        mode="train",
        interval_seconds=900,
        expected_generation=2,
        reason="slow cadence",
        operator_id="operator:local",
    )
    schedule = await _schedule(db)
    assert changed["outcome"] == "enabled"
    assert changed["generation"] == 3
    assert schedule["interval_seconds"] == 900
    assert schedule["next_due_at"] == 950.0
    assert schedule["request_sequence"] == 4
    assert schedule["outstanding_request_id"] == "integration-sweep:p:4"
    assert schedule["outstanding_trigger"] == "manual"
    assert schedule["outstanding_requested_at"] == 21.0
    assert schedule["catchup_trigger"] == "manual"
    assert schedule["catchup_requested_at"] == 22.0
    assert schedule["catchup_after_sequence"] == 4


async def test_cadence_rejections_and_stale_or_draining_requests_write_nothing(db):
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: 70.0)
    for mode in ("disabled", "observe", "hierarchy"):
        with pytest.raises(ValueError, match="only valid with train"):
            await service.enable(
                "p",
                mode=mode,
                interval_seconds=60,
                expected_generation=0,
                reason="invalid combination",
                operator_id="operator:local",
            )
    for interval in (0, -1, True, "60"):
        with pytest.raises(ValueError, match="positive"):
            await service.enable(
                "p",
                mode="train",
                interval_seconds=interval,
                expected_generation=0,
                reason="invalid interval",
                operator_id="operator:local",
            )
    assert await _row_count(db, integration_rollout_transitions) == 0

    await service.enable(
        "p",
        mode="train",
        interval_seconds=300,
        expected_generation=0,
        reason="start",
        operator_id="operator:local",
    )
    before = await _schedule(db)
    stale = await service.enable(
        "p",
        mode="train",
        interval_seconds=600,
        expected_generation=0,
        reason="stale cadence",
        operator_id="operator:local",
    )
    assert stale["outcome"] == "stale"
    assert await _schedule(db) == before

    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(hierarchical_integration_draining=True)
        )
    blocked = await service.enable(
        "p",
        mode="train",
        interval_seconds=600,
        expected_generation=1,
        reason="must not cancel drain",
        operator_id="operator:local",
    )
    assert blocked["outcome"] == "blocked"
    assert blocked["draining"] is True
    assert blocked["blockers"][0]["code"] == "integration_drain_active"
    assert (await db.get_project("p")).hierarchical_integration_generation == 1
    assert await _row_count(db, integration_rollout_transitions) == 1
    assert await _schedule(db) == before


@pytest.mark.parametrize("interval_seconds", [True, "60"])
async def test_raw_enable_handler_does_not_coerce_interval(
    command_handler_factory, interval_seconds
):
    handler = await command_handler_factory()
    handler.orchestrator.integration_control_service = IntegrationControlService(handler.db)

    with pytest.raises(ValueError, match="positive integer"):
        await handler._cmd_integration_enable(
            {
                "project_id": "p",
                "mode": "train",
                "expected_generation": 0,
                "reason": "strict cadence",
                "interval_seconds": interval_seconds,
            }
        )

    await handler.db.close()


async def test_observe_flush_and_scheduler_boundary_never_create_schedule(db):
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: 20.0)
    result = await service.enable(
        "p",
        mode="observe",
        expected_generation=0,
        reason="read-only observation",
        operator_id="operator:local",
    )
    assert result["outcome"] == "enabled"
    flushed = await service.flush("p")
    assert flushed["outcome"] == "eligibility"
    assert flushed["ready"] is True
    direct = await IntegrationScheduler(db).mark_due("p", 20.0, "manual")
    assert direct["outcome"] == "disabled"
    assert await _row_count(db, project_integration_schedules) == 0


@pytest.mark.parametrize("branchless_parent", ["skip", "declared"])
async def test_non_verifier_branchless_policy_has_no_verifier_route_blocker(
    db, branchless_parent
):
    await db.update_project(
        "p", hierarchical_integration_policy=_policy(branchless_parent=branchless_parent)
    )

    preflight = await IntegrationControlService(
        db, external_preflight=_external_ready
    ).preflight("p")

    assert preflight["ready"] is True
    assert preflight["blockers"] == []


async def test_disable_drains_active_batch_then_background_reconciler_restores_legacy(db):
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: 30.0)
    await service.enable(
        "p",
        mode="train",
        expected_generation=0,
        reason="start train",
        operator_id="operator:local",
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request",
                trigger="manual",
                source_manifest_digest="sha256:" + "a" * 64,
                base_sha="b" * 40,
                lifecycle="testing",
                integration_branch="refs/heads/aq/integration/test",
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="pending",
                created_at=30.0,
                updated_at=30.0,
            )
        )

    draining = await service.enable(
        "p",
        mode="disabled",
        expected_generation=1,
        reason="roll back",
        operator_id="operator:local",
    )
    assert draining["outcome"] == "draining"
    project = await db.get_project("p")
    assert project.hierarchical_integration_mode == "train"
    assert project.hierarchical_integration_desired_mode == "disabled"
    assert project.hierarchical_integration_draining is True
    assert (await db.get_integration_legacy_suppression("p"))["merge_sweep_suppressed"] is True
    assert (await IntegrationScheduler(db).mark_due("p", 31.0, "manual"))["outcome"] == "disabled"

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="failed", cleanup_state="complete", updated_at=32.0)
        )
    completed = await service.reconcile_drains(33.0)
    assert completed == ("p",)
    project = await db.get_project("p")
    assert project.hierarchical_integration_mode == "disabled"
    assert project.hierarchical_integration_desired_mode == "disabled"
    assert project.hierarchical_integration_draining is False
    assert (await db.get_integration_legacy_suppression("p"))["merge_sweep_suppressed"] is False


async def test_active_managed_work_rejects_observe_without_restoring_legacy(db):
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: 35.0)
    await service.enable(
        "p",
        mode="train",
        expected_generation=0,
        reason="start train",
        operator_id="operator:local",
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="active-observe-batch",
                project_id="p",
                repository_id="repo",
                request_id="active-observe-request",
                trigger="manual",
                source_manifest_digest="sha256:" + "a" * 64,
                base_sha="b" * 40,
                lifecycle="testing",
                integration_branch="refs/heads/aq/integration/active-observe",
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="pending",
                created_at=35.0,
                updated_at=35.0,
            )
        )

    result = await service.enable(
        "p",
        mode="observe",
        expected_generation=1,
        reason="unsafe downgrade",
        operator_id="operator:local",
    )

    assert result["outcome"] == "blocked"
    assert result["blockers"] == [
        {
            "code": "active_integration_work",
            "detail": "managed integration work must drain before observe mode",
            "ref": "p",
        }
    ]
    project = await db.get_project("p")
    assert project.hierarchical_integration_mode == "train"
    assert project.hierarchical_integration_generation == 1
    assert (await db.get_integration_legacy_suppression("p"))["merge_sweep_suppressed"] is True


async def test_blocked_preflight_returns_structured_digest_and_never_mutates(db):
    async def unavailable(_project_id: str, _repository_id: str) -> tuple[str, ...]:
        return ("provider_not_wired",)

    service = IntegrationControlService(db, external_preflight=unavailable, clock=lambda: 40.0)
    result = await service.enable(
        "p",
        mode="train",
        expected_generation=0,
        reason="must fail closed",
        operator_id="operator:local",
    )
    assert result["outcome"] == "blocked"
    assert result["blockers"] == [
        {"code": "provider_not_wired", "detail": "functional integration dependency is unavailable", "ref": "repo"}
    ]
    assert result["blocker_digest"].startswith("sha256:")
    assert (await db.get_project("p")).hierarchical_integration_generation == 0
    assert await _row_count(db, integration_rollout_transitions) == 0


async def test_history_waiver_exact_digest_is_single_use_and_reuse_rolls_back(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(gates).values(
                id="legacy-gate",
                project_id="p",
                gate_type="pr-merged",
                title="legacy merge",
                status="open",
                created_at=1.0,
            )
        )
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: 45.0)
    observed = await service.preflight("p")
    assert [blocker["code"] for blocker in observed["blockers"]] == [
        "legacy_pr_merge_gate"
    ]
    waiver = await service.waive_history(
        "p",
        reason="accept recorded pre-cutover history",
        blocker_digest=observed["blocker_digest"],
        operator_id="local:operator",
    )
    enabled = await service.enable(
        "p",
        mode="train",
        expected_generation=0,
        reason="cut over with exact history",
        operator_id="local:operator",
        waiver_id=waiver["waiver_id"],
    )
    assert enabled["outcome"] == "enabled"

    with pytest.raises(ValueError, match="stale or already consumed"):
        await service.enable(
            "p",
            mode="observe",
            expected_generation=1,
            reason="must roll back reuse",
            operator_id="local:operator",
            waiver_id=waiver["waiver_id"],
        )
    project = await db.get_project("p")
    assert project.hierarchical_integration_mode == "train"
    assert project.hierarchical_integration_generation == 1
    assert await _row_count(db, integration_rollout_transitions) == 1


async def test_history_waiver_applicability_is_honored_by_later_cutovers(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(gates).values(
                id="historic-gate",
                project_id="p",
                gate_type="pr-merged",
                title="historic merge",
                status="open",
                created_at=1.0,
            )
        )
    service = IntegrationControlService(db, external_preflight=_external_ready, clock=lambda: 46.0)
    observed = await service.preflight("p")
    waiver = await service.waive_history(
        "p",
        reason="accept pre-cutover gate",
        blocker_digest=observed["blocker_digest"],
        operator_id="local:operator",
    )
    first = await service.enable(
        "p",
        mode="hierarchy",
        expected_generation=0,
        reason="first cutover",
        operator_id="local:operator",
        waiver_id=waiver["waiver_id"],
    )
    disabled = await service.enable(
        "p",
        mode="disabled",
        expected_generation=1,
        reason="rollback",
        operator_id="local:operator",
    )

    later = await service.preflight("p")
    second = await service.enable(
        "p",
        mode="train",
        expected_generation=2,
        reason="second cutover",
        operator_id="local:operator",
    )

    assert first["outcome"] == "enabled"
    assert disabled["outcome"] == "disabled"
    assert later["blockers"] == []
    assert second["outcome"] == "enabled"
    assert await _row_count(db, integration_legacy_gate_applicability) == 1


async def test_daemon_functional_preflight_reads_artifact_trust_and_workflow_variables(db):
    trust = {
        "schema": "aq.integration-trust.v1",
        "canonical_repository_id": "repo",
        "repository_id": 303,
        "full_name": "acme/widgets",
        "ci_producer_app_id": 1234,
        "attestation_app_id": 101,
        "attestation_name": "Agent Queue Integration Attestation",
        "required_checks": {"version": "checks-v1", "names": ["Tests (default)"]},
    }

    class Client:
        repository = GitHubRepositoryBinding(303, "acme/widgets")
        config = SimpleNamespace(app_id=101)

        async def request_json(self, _method, path):
            if "/contents/" in path:
                return {
                    "encoding": "base64",
                    "content": base64.b64encode(json.dumps(trust).encode()).decode(),
                }
            value = (
                "101"
                if path.endswith("AQ_INTEGRATION_ATTESTATION_APP_ID")
                else "checks-v1"
            )
            return {"name": path.rsplit("/", 1)[-1], "value": value}

    loaded = SimpleNamespace(
        id="hierarchical-delivery",
        schema_version=2,
        source_hash="sha256:" + "3" * 64,
        version=1,
        contract_fingerprint=lambda: "sha256:" + "2" * 64,
    )
    runtime = SimpleNamespace(_store=SimpleNamespace(load=lambda _sha: loaded))
    orchestrator = SimpleNamespace(
        db=db,
        integration_app_client_factory=lambda _binding: Client(),
        integration_repository_binding_resolver=lambda _repository: Client.repository,
        playbook_manager=runtime,
        integration_attestation_service=object(),
        root_promotion_service=object(),
        integration_cleanup_service=object(),
        git=object(),
        intelligence_classes={"standard": object(), "deep": object()},
    )
    db.list_profiles = AsyncMock(
        return_value=[SimpleNamespace(id=value) for value in ("worker", "debugger", "verifier")]
    )

    assert await daemon_functional_preflight(orchestrator, "p", "repo") == ()

    runtime._store.load = lambda _sha: (_ for _ in ()).throw(FileNotFoundError())
    trust["required_checks"]["version"] = "wrong"
    blockers = await daemon_functional_preflight(orchestrator, "p", "repo")
    assert "route_artifact_unavailable" in blockers
    assert "trust_manifest_mismatch" in blockers
    assert "hosted_workflow_variables_mismatch" in blockers


async def test_daemon_functional_preflight_mints_token_with_variables_read(
    db, monkeypatch
):
    trust = {
        "schema": "aq.integration-trust.v1",
        "canonical_repository_id": "repo",
        "repository_id": 303,
        "full_name": "acme/widgets",
        "ci_producer_app_id": 1234,
        "attestation_app_id": 101,
        "attestation_name": "Agent Queue Integration Attestation",
        "required_checks": {"version": "checks-v1", "names": ["Tests (default)"]},
    }

    class PermissionAwareTransport:
        def __init__(self):
            self.token_permissions = None

        async def request(self, method, url, *, headers, json_body=None, max_bytes):
            if url.endswith("/app"):
                return HttpResponse(200, {}, b'{"id":101}')
            if url.endswith("/app/installations/202/access_tokens"):
                self.token_permissions = json_body["permissions"]
                body = {
                    "token": "installation-secret",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "repositories": [{"id": 303}],
                    "permissions": self.token_permissions,
                }
                return HttpResponse(201, {}, json.dumps(body).encode())
            if url.endswith("/repositories/303"):
                return HttpResponse(
                    200, {}, b'{"id":303,"full_name":"acme/widgets"}'
                )
            if "/contents/" in url:
                body = {
                    "encoding": "base64",
                    "content": base64.b64encode(json.dumps(trust).encode()).decode(),
                }
                return HttpResponse(200, {}, json.dumps(body).encode())
            if "/actions/variables/" in url:
                if (self.token_permissions or {}).get("variables") != "read":
                    return HttpResponse(403, {}, b'{"message":"forbidden"}')
                name = url.rsplit("/", 1)[-1]
                value = (
                    "101"
                    if name == "AQ_INTEGRATION_ATTESTATION_APP_ID"
                    else "checks-v1"
                )
                return HttpResponse(
                    200, {}, json.dumps({"name": name, "value": value}).encode()
                )
            raise AssertionError(f"unexpected GitHub request: {method} {url}")

    transport = PermissionAwareTransport()
    binding = GitHubRepositoryBinding(303, "acme/widgets")
    client = GitHubAppClient(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        binding,
        key_provider=SimpleNamespace(read_private_key=lambda _path: b"unused"),
        transport=transport,
        clock=lambda: 1_800_000_000.0,
    )
    monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
    loaded = SimpleNamespace(
        id="hierarchical-delivery",
        schema_version=2,
        source_hash="sha256:" + "3" * 64,
        version=1,
        contract_fingerprint=lambda: "sha256:" + "2" * 64,
    )
    orchestrator = SimpleNamespace(
        db=db,
        integration_app_client_factory=lambda _binding: client,
        integration_repository_binding_resolver=lambda _repository: binding,
        playbook_manager=SimpleNamespace(
            _store=SimpleNamespace(load=lambda _sha: loaded)
        ),
        integration_attestation_service=object(),
        root_promotion_service=object(),
        integration_cleanup_service=object(),
        git=object(),
        intelligence_classes={"standard": object(), "deep": object()},
    )
    db.list_profiles = AsyncMock(
        return_value=[
            SimpleNamespace(id=value)
            for value in ("worker", "debugger", "verifier")
        ]
    )

    assert await daemon_functional_preflight(orchestrator, "p", "repo") == ()
    assert transport.token_permissions == {
        "checks": "write",
        "actions": "read",
        "contents": "write",
        "administration": "read",
        "pull_requests": "write",
        "issues": "write",
        "variables": "read",
    }


async def test_status_reports_functional_readiness_and_deferred_certification(db):
    status = await IntegrationStatusService(db).status("p")

    assert status["rollout_ready"] is True
    assert status["blockers"] == []
    assert status["blocker_digest"] == (
        "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    assert status["certification"]["status"] == "not_performed"


async def test_public_control_authority_keeps_enable_local_and_status_project_scoped(
    command_handler_factory,
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="project"))
    controls = AsyncMock()
    controls.status.return_value = {"outcome": "status", "project_id": "p"}
    controls.enable.return_value = {"outcome": "enabled", "project_id": "p"}
    controls.flush.return_value = {"outcome": "eligibility", "project_id": "p"}
    controls.resume.return_value = {"outcome": "resumed", "operation_id": "op"}
    handler.orchestrator.integration_control_service = controls

    local = await handler.execute(
        "integration_enable",
        {
            "project_id": "p",
            "mode": "train",
            "interval_seconds": 600,
            "expected_generation": 0,
            "reason": "operator requested",
        },
    )
    assert local["outcome"] == "enabled"
    controls.enable.assert_awaited_once_with(
        "p",
        mode="train",
        interval_seconds=600,
        expected_generation=0,
        reason="operator requested",
        operator_id="local:-",
        waiver_id=None,
    )

    elevated = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["integration_enable", "integration_resume"]
        ),
        project_id="p",
        session_id="supervisor-p",
        elevated=True,
    )
    with principal_context(elevated):
        denied = await handler.execute(
            "integration_enable",
            {
                "project_id": "p",
                "mode": "observe",
                "expected_generation": 1,
                "reason": "must not inherit local authority",
            },
        )
    assert denied["outcome"] == "unauthorized"
    assert controls.enable.await_count == 1
    with principal_context(elevated):
        denied_resume = await handler.execute(
            "integration_resume", {"operation_id": "op"}
        )
    assert denied_resume["outcome"] == "unauthorized"
    controls.resume.assert_not_awaited()

    same_project = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_status"]),
        project_id="p",
        session_id="reader",
    )
    with principal_context(same_project):
        status = await handler.execute("integration_status", {"project_id": "p"})
    assert status["outcome"] == "status"

    wrong_project = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_status"]),
        project_id="other",
        session_id="reader",
    )
    with principal_context(wrong_project):
        denied_status = await handler.execute("integration_status", {"project_id": "p"})
    assert denied_status["outcome"] == "unauthorized"

    capable_playbook = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["integration_flush"]),
        project_id="p",
    )
    with principal_context(capable_playbook):
        flush = await handler.execute("integration_flush", {"project_id": "p"})
    assert flush["outcome"] == "eligibility"
    await handler.db.close()


async def test_sensitive_project_configuration_is_local_generation_cas_and_delete_is_guarded(
    command_handler_factory,
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="project"))
    await handler.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
        )
    )
    controls = AsyncMock()
    controls.configure.return_value = {
        "outcome": "configured",
        "project_id": "p",
        "generation": 1,
    }
    handler.orchestrator.integration_control_service = controls

    configured = await handler.execute(
        "edit_project",
        {
            "project_id": "p",
            "integration_repository_id": "repo",
            "hierarchical_integration_policy": _policy(),
            "expected_integration_generation": 0,
        },
    )
    assert configured["outcome"] == "configured"

    elevated = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["edit_project"]),
        project_id="p",
        session_id="supervisor-p",
        elevated=True,
    )
    with principal_context(elevated):
        denied = await handler.execute(
            "edit_project",
            {
                "project_id": "p",
                "integration_repository_id": "repo",
                "expected_integration_generation": 1,
            },
        )
    assert "LOCAL" in denied["error"]
    assert controls.configure.await_count == 1

    rejected = await handler.execute(
        "edit_project",
        {"project_id": "p", "hierarchical_integration_mode": "train"},
    )
    assert "integration_enable" in rejected["error"]

    await handler.db.update_project("p", hierarchical_integration_mode="observe")
    deletion = await handler.execute("delete_project", {"project_id": "p"})
    assert "disabled and drained" in deletion["error"]
    assert await handler.db.get_project("p") is not None
    await handler.db.close()


async def test_human_resume_reconciles_ambiguous_publication_and_abort_is_db_only(db):
    repair_policy = RepairPolicy(
        primary_seconds=60,
        debug_seconds=120,
        debug_intelligence_class="deep",
        debug_profile_id="debugger",
    ).model_dump(mode="json")
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="human-batch",
                project_id="p",
                repository_id="repo",
                request_id="human-request",
                trigger="manual",
                source_manifest_digest="sha256:" + "a" * 64,
                base_sha="b" * 40,
                lifecycle="human_blocked",
                integration_branch="refs/heads/aq/integration/human",
                repair_stage_ordinal=1,
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="pending",
                created_at=50.0,
                updated_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="human-batch",
                revision=0,
                construction_base_sha="b" * 40,
                state="red",
                created_at=50.0,
                updated_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="human-operation",
                target_kind="batch",
                batch_id="human-batch",
                episode_id="episode",
                active_stage=1,
                state="human_required",
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                required_check_version="checks-v1",
                created_at=50.0,
                updated_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="human-operation",
                ordinal=1,
                policy=repair_policy,
                intelligence_class="deep",
                profile_id="debugger",
                starting_sha="b" * 40,
                attempts=3,
                dossier={},
                state="failed",
                completed_at=50.0,
            )
        )
        await conn.execute(
            insert(integration_candidate_publications).values(
                batch_id="human-batch",
                revision=0,
                state="reserved",
                repository_id="repo",
                repository_numeric_id=1234,
                repository_full_name="acme/widgets",
                base_ref="main",
                head_ref="aq/integration/human",
                head_sha="c" * 40,
                expected_old_sha="b" * 40,
                idempotency_key="human-publication",
                created_at=50.0,
                updated_at=50.0,
            )
        )

    service = IntegrationControlService(db, clock=lambda: 60.0)
    ambiguous = await service.resume("human-operation")
    assert ambiguous["outcome"] == "ambiguous"
    assert ambiguous["blockers"] == [
        {
            "code": "ambiguous_external_write",
            "detail": "operation has unresolved external mutation evidence",
            "ref": "candidate_publication",
        }
    ]

    async with db.immediate() as conn:
        await conn.execute(
            update(integration_candidate_publications)
            .where(integration_candidate_publications.c.batch_id == "human-batch")
            .values(state="ref_published", updated_at=60.0)
        )
        await conn.execute(
            update(integration_candidate_publications)
            .where(integration_candidate_publications.c.batch_id == "human-batch")
            .values(state="pr_reserved", updated_at=60.0)
        )
        await conn.execute(
            update(integration_candidate_publications)
            .where(integration_candidate_publications.c.batch_id == "human-batch")
            .values(
                state="pr_published",
                pr_number=17,
                pr_url="https://github.com/acme/widgets/pull/17",
                updated_at=60.0,
            )
        )
    resumed = await service.resume("human-operation")
    assert resumed == {
        "outcome": "resumed",
        "operation_id": "human-operation",
        "project_id": "p",
        "state": "escalated",
        "stage": 1,
        "deadline_at": 180.0,
    }
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "human-operation")
            .values(state="human_required")
        )
        await conn.execute(
            update(integration_repair_stages)
            .where(integration_repair_stages.c.operation_id == "human-operation")
            .values(state="failed", completed_at=61.0)
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "human-batch")
            .values(lifecycle="human_blocked")
        )
    aborted = await service.abort("human-operation", reason="retain for diagnosis")
    assert aborted["outcome"] == "aborted"
    batch = await db.get_integration_batch("human-batch")
    assert batch["lifecycle"] == "aborted"
    assert batch["human_abort_reason"] == "retain for diagnosis"


async def test_cleanup_retry_requeues_exact_safe_work_and_preserves_prewrite(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="cleanup-batch",
                project_id="p",
                repository_id="repo",
                request_id="cleanup-request",
                trigger="manual",
                source_manifest_digest="sha256:" + "d" * 64,
                base_sha="a" * 40,
                lifecycle="promoted",
                integration_branch="refs/heads/aq/integration/cleanup",
                final_main_sha="b" * 40,
                policy_snapshot=_policy(),
                artifact_snapshot=_artifact().model_dump(mode="json"),
                cleanup_state="conflict",
                created_at=70.0,
                updated_at=70.0,
            )
        )
        await conn.execute(
            insert(integration_cleanup_items).values(
                batch_id="cleanup-batch",
                kind="remote_ref",
                identity="safe",
                domain_key="cleanup:safe",
                project_id="p",
                repository_id="repo",
                repository_numeric_id=1234,
                repository_full_name="acme/widgets",
                revision=0,
                target_ref="refs/heads/aq/integration/safe",
                expected_sha="b" * 40,
                state="retryable",
                attempts=3,
                next_attempt_at=999.0,
                created_at=70.0,
                updated_at=70.0,
            )
        )
    service = IntegrationControlService(db, clock=lambda: 80.0)
    retried = await service.retry_cleanup("cleanup-batch")
    assert retried["outcome"] == "requeued"
    assert retried["count"] == 1
    async with db._engine.connect() as conn:
        safe = (
            await conn.execute(
                select(integration_cleanup_items).where(
                    integration_cleanup_items.c.domain_key == "cleanup:safe"
                )
            )
        ).mappings().one()
    assert safe["attempts"] == 0
    assert safe["next_attempt_at"] == 80.0

    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_cleanup_items).values(
                batch_id="cleanup-batch",
                kind="remote_ref",
                identity="ambiguous",
                domain_key="cleanup:ambiguous",
                project_id="p",
                repository_id="repo",
                repository_numeric_id=1234,
                repository_full_name="acme/widgets",
                revision=0,
                target_ref="refs/heads/aq/integration/ambiguous",
                expected_sha="b" * 40,
                state="retryable",
                attempts=1,
                next_attempt_at=999.0,
                irreversible_nonce="posted",
                irreversible_prewrite_at=79.0,
                created_at=70.0,
                updated_at=79.0,
            )
        )
    blocked = await service.retry_cleanup("cleanup-batch")
    assert blocked["outcome"] == "ambiguous"
    assert blocked["blockers"] == [
        {
            "code": "cleanup_irreversible",
            "detail": "cleanup item has an unresolved irreversible write marker",
            "ref": "cleanup:ambiguous",
        }
    ]
    async with db._engine.connect() as conn:
        marker = (
            await conn.execute(
                select(integration_cleanup_items.c.irreversible_prewrite_at).where(
                    integration_cleanup_items.c.domain_key == "cleanup:ambiguous"
                )
            )
        ).scalar_one()
    assert marker == 79.0
