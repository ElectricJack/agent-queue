"""Disabled reviewed root train policy and actual command routing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import insert

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
)
from src.integration.candidates import CandidateBuildResult
from src.integration.candidates import CandidateService
from src.git.github_app import GitHubRepositoryBinding
from src.integration.main_promotion import RootPromotionResult
from src.integration.release import IntegrationReleaseResult
from src.models import Project, RepoConfig, RepoSourceType
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.profiles.capabilities import CapabilityPolicy
from src.vault import ensure_default_playbooks
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)


FIXTURE = Path("tests/fixtures/playbooks/v2/root-integration-train/artifact.json")
SOURCE = Path("src/prompts/default_playbooks/root-integration-train.md")


def _artifact():
    return load_definition_json(FIXTURE.read_text(encoding="utf-8"))


def test_root_train_source_is_seeded_disabled_and_never_overwrites(tmp_path):
    first = ensure_default_playbooks(str(tmp_path))
    installed = tmp_path / "vault/system/playbooks/root-integration-train.md"
    assert "root-integration-train.md" in first["created"]
    assert installed.read_bytes() == SOURCE.read_bytes()
    assert "enabled: false" in SOURCE.read_text(encoding="utf-8")
    installed.write_text("operator-owned\n", encoding="utf-8")
    second = ensure_default_playbooks(str(tmp_path))
    assert "root-integration-train.md" in second["skipped"]
    assert installed.read_text(encoding="utf-8") == "operator-owned\n"


def test_reviewed_root_routes_use_only_subject_commands():
    artifact = _artifact()
    entries = {rule.id: artifact.steps[rule.entry_step] for rule in artifact.rules}
    assert entries["seal-due-frontier"].command == "integration_seal"
    assert set(entries["seal-due-frontier"].inputs) == {"project_id", "request_id"}
    assert entries["construct-and-test"].command == "integration_build_candidate"
    assert set(entries["construct-and-test"].inputs) == {"batch_id"}
    assert entries["release-promoted"].command == "integration_release"
    assert set(entries["release-promoted"].inputs) == {"batch_id"}
    assert entries["cleanup-promoted"].command == "integration_cleanup"
    assert set(entries["cleanup-promoted"].inputs) == {"batch_id"}


def test_candidate_result_routes_are_durable_and_server_derive_repair_stage():
    artifact = _artifact()
    rules = {rule.id: rule for rule in artifact.rules}
    entries = {rule_id: artifact.steps[rule.entry_step] for rule_id, rule in rules.items()}

    assert rules["promote-green-candidate"].trigger.event_type == "integration.candidate_green"
    assert entries["promote-green-candidate"].command == "integration_promote_main"
    assert set(entries["promote-green-candidate"].inputs) == {"batch_id", "revision"}
    assert rules["repair-red-candidate"].trigger.event_type == "integration.candidate_red"
    assert entries["repair-red-candidate"].command == "integration_repair_dispatch"
    assert set(entries["repair-red-candidate"].inputs) == {
        "operation_id",
        "batch_id",
        "revision",
        "head_sha",
    }

    construct = entries["construct-and-test"]
    conflict_step = artifact.steps[construct.transitions["conflict"]]
    assert conflict_step.command == "integration_repair_dispatch"
    assert set(conflict_step.inputs) == {"operation_id"}


async def test_due_and_cleanup_events_run_real_executor_and_subject_handlers(
    command_handler_factory, monkeypatch
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="project"))
    await handler.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_batches),
            {
                    "id": "empty-batch", "project_id": "p", "repository_id": "repo",
                    "request_id": "request-1", "source_manifest_digest": "digest-empty",
                    "lifecycle": "empty", "current_revision": 0, "policy_snapshot": {},
                    "artifact_snapshot": {}, "cleanup_state": "complete", "created_at": 1.0,
                    "updated_at": 1.0,
                },
        )
        await conn.execute(
            insert(integration_batches),
            {
                "id": "build-batch", "project_id": "p", "repository_id": "repo",
                "request_id": "request-2", "source_manifest_digest": "digest-build",
                "base_sha": "a" * 40, "lifecycle": "sealed", "current_revision": 0,
                "integration_branch": "refs/heads/aq/integration/build",
                "policy_snapshot": {}, "artifact_snapshot": {}, "cleanup_state": "pending",
                "created_at": 1.0, "updated_at": 1.0,
            },
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="build-batch", revision=0, construction_base_sha="a" * 40,
                next_member_ordinal=1, head_sha="b" * 40, state="built",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="root-op", target_kind="batch", batch_id="build-batch",
                episode_id="build-batch", active_stage=1, state="active",
                policy_snapshot={}, artifact_snapshot={}, required_check_version="checks-v1",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id="root-op", ordinal=1, policy={}, starting_sha="b" * 40,
                attempts=0, state="active",
            )
        )
        await conn.execute(
            insert(integration_batches),
            {
                    "id": "promoted-batch", "project_id": "p", "repository_id": "repo",
                    "request_id": "request-0", "source_manifest_digest": "digest-promoted",
                    "base_sha": "a" * 40, "lifecycle": "promoted", "current_revision": 0,
                    "integration_branch": "refs/heads/aq/integration/test",
                    "final_main_sha": "b" * 40, "policy_snapshot": {}, "artifact_snapshot": {},
                    "cleanup_state": "pending", "created_at": 1.0, "updated_at": 1.0,
                },
        )
    train = AsyncMock()
    train.seal.return_value = {
        "outcome": "empty", "project_id": "p", "request_id": "request-1",
        "batch_id": "empty-batch", "operation_id": None,
    }
    release = AsyncMock()
    release.release.side_effect = (
        lambda batch_id, _now: IntegrationReleaseResult(
            outcome="empty" if batch_id == "empty-batch" else "released",
            project_id="p",
            batch_id=batch_id,
            request_id="request-1" if batch_id == "empty-batch" else "request-0",
            operation_id=None if batch_id == "empty-batch" else "root-op",
        )
    )
    cleanup = AsyncMock()
    cleanup.materialize.return_value = type(
        "Materialized", (), {"outcome": "materialized", "item_count": 0}
    )()
    cleanup.advance.return_value = []
    handler.orchestrator.integration_train_service = train
    handler.orchestrator.integration_release_service = release
    handler.orchestrator.integration_cleanup_service = cleanup
    candidate = AsyncMock()
    candidate.build.return_value = CandidateBuildResult(
        outcome="built", batch_id="build-batch", revision=0, operation_id="root-op",
        head_sha="b" * 40, branch="refs/heads/aq/integration/build",
    )
    attestation = AsyncMock()
    attestation.handle_candidate_ci.return_value = {
        "outcome": "not_green", "evidence_ids": [],
    }
    promotion = AsyncMock()
    promotion.promote.return_value = RootPromotionResult(
        outcome="promoted", batch_id="build-batch", revision=0,
        intent_id="intent", receipt_ids=("receipt",), head_sha="b" * 40,
    )
    handler.orchestrator.integration_candidate_service = candidate
    handler.orchestrator.integration_attestation_service = attestation
    handler.orchestrator.root_promotion_service = promotion
    repair = AsyncMock()
    repair.dispatch.return_value = {"outcome": "dispatched", "stage": 1}
    handler.orchestrator.repair_service = repair
    monkeypatch.setattr("src.commands.integration_commands.time.time", lambda: 123.0)

    artifact = _artifact()
    ref = artifact_ref_for(artifact)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 123.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=handler.db,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([ref]),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=[
                "integration_seal", "integration_release", "integration_cleanup",
                "integration_build_candidate", "integration_ci_evidence",
                "integration_promote_main", "integration_repair_dispatch",
            ]
        ),
    )
    set_handler_provider(lambda: handler)
    try:
        due = await engine.dispatch_event(
            {
                "event_type": "integration.sweep_due", "event_id": "due-1",
                "project_id": "p", "operation_id": "request-1",
            },
            principal,
        )
        cleaned = await engine.dispatch_event(
            {
                "event_type": "integration.cleanup_requested", "event_id": "cleanup-1",
                "project_id": "p", "operation_id": "root-op", "batch_id": "promoted-batch",
                "revision": 0, "intent_id": "intent", "head_sha": "b" * 40,
            },
            principal,
        )
        built = await engine.dispatch_event(
            {
                "event_type": "integration.sealed", "event_id": "sealed-1",
                "project_id": "p", "operation_id": "root-op", "batch_id": "build-batch",
            },
            principal,
        )
        green = await engine.dispatch_event(
            {
                "event_type": "integration.candidate_green",
                "event_id": "green-1",
                "project_id": "p",
                "operation_id": "root-op",
                "batch_id": "build-batch",
                "revision": 0,
                "head_sha": "b" * 40,
            },
            principal,
        )
        red = await engine.dispatch_event(
            {
                "event_type": "integration.candidate_red",
                "event_id": "red-1",
                "project_id": "p",
                "operation_id": "root-op",
                "batch_id": "build-batch",
                "revision": 0,
                "head_sha": "b" * 40,
            },
            principal,
        )
        released = await engine.dispatch_event(
            {
                "event_type": "integration.batch_promoted", "event_id": "promoted-1",
                "project_id": "p", "operation_id": "root-op",
                "batch_id": "promoted-batch", "revision": 0,
                "intent_id": "intent", "head_sha": "b" * 40,
            },
            principal,
        )
        debug = await engine.dispatch_event(
            {
                "event_type": "integration.repair_exhausted", "event_id": "repair-1",
                "project_id": "p", "operation_id": "root-op", "stage": 0,
            },
            principal,
        )
    finally:
        set_handler_provider(None)
    assert due.rules_selected == ("seal-due-frontier",)
    assert cleaned.rules_selected == ("cleanup-promoted",)
    assert built.rules_selected == ("construct-and-test",)
    assert green.rules_selected == ("promote-green-candidate",)
    assert red.rules_selected == ("repair-red-candidate",)
    assert released.rules_selected == ("release-promoted",)
    assert debug.rules_selected == ("dispatch-debug",)
    train.seal.assert_awaited_once_with("p", "request-1", 123.0)
    assert release.release.await_args_list == [
        (("empty-batch", 123.0), {}),
        (("promoted-batch", 123.0), {}),
    ]
    cleanup.materialize.assert_awaited_once_with("promoted-batch")
    cleanup.advance.assert_awaited_once_with("promoted-batch")
    candidate.build.assert_awaited_once_with("build-batch")
    attestation.handle_candidate_ci.assert_awaited_once()
    promotion.promote.assert_awaited_once_with("build-batch", 0)
    assert repair.dispatch.await_args_list == [(("root-op", 1), {}), (("root-op", 1), {})]
    assert {snapshot.lifecycle.value for snapshot in runs.snapshots.values()} == {"completed"}
    await handler.db.close()


async def test_green_event_rebuild_conflict_dispatches_existing_stage_through_engine(
    command_handler_factory, monkeypatch
):
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="project"))
    await handler.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_batches),
            {
                "id": "build-batch",
                "project_id": "p",
                "repository_id": "repo",
                "request_id": "request-2",
                "source_manifest_digest": "digest-build",
                "base_sha": "a" * 40,
                "lifecycle": "building",
                "current_revision": 0,
                "integration_branch": "refs/heads/aq/integration/build",
                "policy_snapshot": {},
                "artifact_snapshot": {},
                "cleanup_state": "pending",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )
        await conn.execute(
            insert(integration_candidate_revisions).values(
                batch_id="build-batch",
                revision=0,
                construction_base_sha="a" * 40,
                next_member_ordinal=1,
                head_sha="b" * 40,
                state="green",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="root-op",
                target_kind="batch",
                batch_id="build-batch",
                episode_id="build-batch",
                active_stage=0,
                state="active",
                policy_snapshot={},
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
                starting_sha="a" * 40,
                attempts=0,
                state="active",
            )
        )
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="superseded-intent",
                domain_key="root:build-batch:0",
                operation_key="root-op",
                project_id="p",
                receipt_id="superseded-receipt",
                source_head="b" * 40,
                source_base="a" * 40,
                repository_id="repo",
                target_branch="refs/heads/main",
                expected_target="a" * 40,
                fence_owner_id="root-op",
                fence_token=0,
                state="superseded",
                intent_kind="root",
                root_batch_id="build-batch",
                root_candidate_revision=0,
                project_lease_owner_id="root-op",
                project_lease_fence_token=0,
                branch_fence_owner_id="root-op",
                branch_fence_token=0,
                ci_evidence_id="ci",
                created_at=1.0,
                updated_at=1.0,
            )
        )

    promotion = AsyncMock()
    promotion.promote.return_value = RootPromotionResult(
        outcome="base_moved",
        batch_id="build-batch",
        revision=0,
        intent_id="intent",
        head_sha="b" * 40,
    )
    candidate = AsyncMock()
    candidate.app_client = SimpleNamespace(
        exact_head_ref=AsyncMock(return_value="d" * 40)
    )
    candidate._repository = AsyncMock(
        return_value=RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            default_branch="main",
        )
    )
    candidate.rebuild.return_value = CandidateBuildResult(
        outcome="conflict",
        batch_id="build-batch",
        revision=1,
        operation_id="root-op",
        head_sha="c" * 40,
        branch="refs/heads/aq/integration/build",
    )
    repair = AsyncMock()
    repair.dispatch.return_value = {"outcome": "dispatched", "stage": 0}
    handler.orchestrator.root_promotion_service = promotion
    handler.orchestrator.integration_candidate_service = candidate
    handler.orchestrator.repair_service = repair
    monkeypatch.setattr("src.commands.integration_commands.time.time", lambda: 123.0)

    artifact = _artifact()
    ref = artifact_ref_for(artifact)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 123.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=handler.db,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([ref]),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=[
                "integration_promote_main",
                "integration_build_candidate",
                "integration_repair_dispatch",
            ]
        ),
    )
    set_handler_provider(lambda: handler)
    try:
        result = await engine.dispatch_event(
            {
                "event_type": "integration.candidate_green",
                "event_id": "green-rebuild-conflict",
                "project_id": "p",
                "operation_id": "root-op",
                "batch_id": "build-batch",
                "revision": 0,
                "head_sha": "b" * 40,
            },
            principal,
        )
    finally:
        set_handler_provider(None)

    assert result.rules_selected == ("promote-green-candidate",)
    promotion.promote.assert_awaited_once_with("build-batch", 0)
    candidate.rebuild.assert_awaited_once_with("build-batch", 0, "d" * 40)
    candidate.build.assert_not_awaited()
    repair.dispatch.assert_awaited_once_with("root-op", 0)
    assert {snapshot.lifecycle.value for snapshot in runs.snapshots.values()} == {"completed"}
    await handler.db.close()


async def test_default_build_command_constructs_repository_bound_candidate_service(
    command_handler_factory, monkeypatch
):
    """A configured daemon must not depend on a test-injected CandidateService."""
    handler = await command_handler_factory()
    await handler.db.create_project(Project(id="p", name="project"))
    await handler.db.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/widgets.git",
            default_branch="main",
        )
    )
    async with handler.db.immediate() as conn:
        await conn.execute(
            insert(integration_batches),
            {
                "id": "build-batch",
                "project_id": "p",
                "repository_id": "repo",
                "request_id": "request-1",
                "source_manifest_digest": "digest-build",
                "base_sha": "a" * 40,
                "lifecycle": "sealed",
                "current_revision": 0,
                "integration_branch": "refs/heads/aq/integration/build",
                "policy_snapshot": {},
                "artifact_snapshot": {},
                "cleanup_state": "pending",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )

    binding = GitHubRepositoryBinding(101, "acme/widgets")
    app = SimpleNamespace(repository=binding)
    resolver = AsyncMock(return_value=binding)
    factory = AsyncMock(return_value=app)
    handler.orchestrator.integration_repository_binding_resolver = resolver
    handler.orchestrator.integration_app_client_factory = factory
    handler.orchestrator.integration_candidate_service = None

    async def built(service, batch_id):
        assert isinstance(service, CandidateService)
        assert service.app_client is app
        assert service.forge_provider is app
        return CandidateBuildResult(
            outcome="built",
            batch_id=batch_id,
            revision=0,
            operation_id=None,
            head_sha="b" * 40,
            branch="refs/heads/aq/integration/build",
        )

    monkeypatch.setattr(CandidateService, "build", built)

    result = await handler._cmd_integration_build_candidate({"batch_id": "build-batch"})

    assert result["outcome"] == "built"
    resolver.assert_awaited_once()
    factory.assert_awaited_once_with(binding)
    await handler.db.close()
