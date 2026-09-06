"""Operational rollout controls for hierarchical integration."""

from __future__ import annotations

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
    integration_repair_operations,
    integration_repair_stages,
    integration_rollout_transitions,
    project_integration_schedules,
)
from src.integration.controls import IntegrationControlService
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


def _policy() -> dict:
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
        verifier_intelligence_class="standard",
        verifier_profile_id="verifier",
    )
    return HierarchicalIntegrationPolicy(
        parent=boundary,
        root=boundary,
        branchless_parent="verifier",
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
            "mode": "observe",
            "expected_generation": 0,
            "reason": "operator requested",
        },
    )
    assert local["outcome"] == "enabled"

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
    assert ambiguous["blockers"] == ["candidate_publication"]

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
    assert blocked["blockers"] == ["cleanup:ambiguous"]
    async with db._engine.connect() as conn:
        marker = (
            await conn.execute(
                select(integration_cleanup_items.c.irreversible_prewrite_at).where(
                    integration_cleanup_items.c.domain_key == "cleanup:ambiguous"
                )
            )
        ).scalar_one()
    assert marker == 79.0
