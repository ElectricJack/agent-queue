"""Crash/replay, ambiguous charges, scope policy, independent budgets and proposal safety."""

import asyncio
import copy
import importlib
from datetime import timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select, update

from src.commands.principal import TRUSTED_LOCAL, principal_context, ExecutionPrincipal
from src.config import AppConfig
from src.database.tables import knowledge_proposals, knowledge_records, record_source_artifacts
from src.knowledge.extraction import (
    ExtractionProviderRegistry,
    ExtractionWorker,
    KnowledgeGenerationLoop,
)
from src.knowledge.extraction_diagnostics import generation_status
from src.knowledge.generation_port import MemoryExtractionPort
from src.knowledge.models import canonical_bytes
from src.knowledge.service import KnowledgeService
from tests.record_helpers import generation_setup, snapshot
from tests.test_knowledge_capture import source


class FakeProvider:
    provider_id, provider_version, available = "fake", "test:1", True

    def __init__(self):
        self.calls = []
        self.estimates = []
        self.result = dict(
            outputs=[
                dict(
                    title="Proposed explanation", body="Unverified hypothesis.", category="incident"
                )
            ],
            actual_microusd=10,
            actual_tokens=500,
        )
        self.failure = None

    def estimate(self, request):
        self.estimates.append(request)
        return dict(microusd=20, tokens=max(600, len(canonical_bytes(request.wire()))))

    async def generate(self, request):
        self.calls.append(request)
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.result)


@pytest.fixture
async def setup(reuse_database, tmp_path):
    db, config, now, store, capture = await generation_setup(reuse_database, tmp_path)
    provider = FakeProvider()
    worker = ExtractionWorker(
        db,
        lambda: config,
        store=store,
        registry=ExtractionProviderRegistry(lambda name: provider),
        timeout=0.1,
    )
    return db, config, now, store, capture, provider, worker


async def job_state(db, store):
    async with db.immediate() as conn:
        return dict((await conn.execute(select(store.jobs))).mappings().one())


async def test_generation_only_commits_unverified_proposals_and_exact_retained_sources(setup):
    db, _, _, store, capture, provider, worker = setup
    await capture.capture([source()])
    assert await worker.tick() == ["succeeded"]
    assert len(provider.calls) == 1
    assert provider.calls[0].inputs[0]["evidence_type"] == "observed"
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_records)) == 0
        proposal = (await conn.execute(select(knowledge_proposals))).mappings().one()
        assert proposal["state"] == "pending"
        doc = proposal["proposed_snapshot"]
        assert doc["verification"] == "unverified" and doc["last_verified_at"] is None
        assert len(doc["sources"]) == 2
        assert doc["metadata"]["extraction.evidence_types"] == ["observed"]
        for item in doc["sources"]:
            await capture.artifacts().read_on(item["artifact_id"], scope_key="project:p", conn=conn)
        assert (await store.reservation_on((await job_state(db, store))["job_id"], conn=conn))[
            "state"
        ] == "settled"
    assert await worker.tick() == []


async def test_crash_after_saved_output_replays_without_provider_or_second_charge(
    setup, monkeypatch
):
    db, _, now, store, capture, provider, worker = setup
    await capture.capture([source()])
    original = worker._publish

    async def crash(job):
        raise RuntimeError("hard crash before proposal transaction")

    monkeypatch.setattr(worker, "_publish", crash)
    assert await worker.tick() == ["interrupted"]
    job = await job_state(db, store)
    assert job["state"] == "leased" and job["result_artifact_id"]
    now[0] += timedelta(seconds=31)
    provider.available = False
    monkeypatch.setattr(worker, "_publish", original)
    assert await worker.tick() == ["succeeded"]
    assert len(provider.calls) == 1 and len(provider.estimates) == 1
    status = await generation_status(db)
    assert status["budgets"][0]["spent_microusd"] == 10
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_proposals)) == 1


async def test_crash_after_begin_before_return_quarantines_and_keeps_allowance(setup):
    db, _, now, store, capture, provider, worker = setup
    job = (await capture.capture([source()]))[0]
    async with db.immediate() as conn:
        job = (await store.claim_due_on(scope_keys=["project:p"], conn=conn))[0]
        await store.configure_budget_on(
            scope_key="project:p",
            feature="extraction",
            limit_microusd=100,
            token_limit=1000,
            conn=conn,
        )
        await store.reserve_on(
            job["job_id"],
            job["lease_token"],
            feature="extraction",
            estimated_microusd=20,
            estimated_tokens=600,
            conn=conn,
        )
        await store.begin_operation_on(
            job["job_id"],
            job["lease_token"],
            provider_operation_id=f"knowledge:{job['job_id']}",
            conn=conn,
        )
    now[0] += timedelta(seconds=31)
    assert await worker.tick() == []
    status = await generation_status(db)
    assert status["unknown_calls"] == 1
    assert status["budgets"][0]["reserved_microusd"] == 20
    assert (await job_state(db, store))["error_code"] == "provider_outcome_unknown"
    assert provider.calls == []


async def test_timeout_with_unknown_provider_outcome_never_retries_paid_call(setup):
    db, _, now, store, capture, provider, worker = setup
    provider.failure = TimeoutError("unknown charge")
    await capture.capture([source()])
    assert await worker.tick() == ["quarantined"]
    assert (await job_state(db, store))["state"] == "quarantined"
    now[0] += timedelta(days=1)
    assert await worker.tick() == []
    assert len(provider.calls) == 1
    status = await generation_status(db)
    assert status["unknown_calls"] == 1 and status["budgets"][0]["reserved_tokens"] == 600


async def test_zero_default_money_allowance_blocks_generation_but_keeps_inputs(setup):
    db, config, _, store, capture, provider, worker = setup
    config.knowledge.extraction.daily_microusd = 0
    await capture.capture([source()])
    assert await worker.tick() == ["retry"]
    assert provider.calls == []
    assert (await job_state(db, store))["error_code"] == "extraction.budget_disabled"
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(store.inputs)) == 1


async def test_missing_provider_retries_without_losing_source_or_reserving_money(setup):
    db, _, now, store, capture, provider, worker = setup
    provider.available = False
    await capture.capture([source()])
    assert await worker.tick() == ["retry"]
    assert (await job_state(db, store))["error_code"] == "provider_unavailable"
    assert (await generation_status(db))["budgets"] == []
    provider.available = True
    now[0] += timedelta(seconds=2)
    assert await worker.tick() == ["succeeded"]


@pytest.mark.parametrize(
    "kind",
    [
        "scope",
        "policy",
        "provider",
        "secret",
        "redaction",
        "tokens",
        "credential_uri",
        "env_secret",
        "github_token",
    ],
)
async def test_outbound_rejects_current_policy_scope_secret_and_size_failures(setup, kind):
    db, config, _, store, capture, provider, worker = setup
    changes = {}
    if kind == "scope":
        changes["scope_key"] = "project:q"
        config.knowledge.enabled_projects = ["p"]
        # Capture while q is allowed, then revoke before outbound.
        config.knowledge.enabled_projects.append("q")
    if kind == "policy":
        changes["source_policy"] = "old-policy"
    if kind == "provider":
        changes["provider_id"] = "unapproved"
    if kind == "secret":
        changes["content"] = "api_key=synthetic-secret-do-not-transfer"
    if kind == "credential_uri":
        changes["content"] = "A logged URL: postgres://test:synthetic-password@host/db"
    if kind == "env_secret":
        changes["content"] = '"AGENT_QUEUE_DB_DSN": "synthetic-do-not-transfer"'
    if kind == "github_token":
        changes["content"] = "A logged token: ghp_synthetic0123456789secret"
    if kind == "tokens":
        changes["content"] = "x" * 8001
    await capture.capture([source(**changes)])
    if kind == "scope":
        config.knowledge.enabled_projects = ["p"]
    if kind == "redaction":
        async with db.immediate() as conn:
            await conn.execute(update(record_source_artifacts).values(redacted_at=store.clock()))
    await worker.tick()
    assert provider.calls == []
    if kind != "scope":
        assert (await job_state(db, store))["state"] == "quarantined"
    else:
        assert (await job_state(db, store))["state"] == "pending"


async def test_budget_reloaded_during_estimation_is_checked_at_admission(setup, monkeypatch):
    from dataclasses import replace

    db, config, _, store, capture, provider, worker = setup
    original = provider.estimate

    def revoke(request):
        estimate = original(request)
        config.knowledge.extraction = replace(config.knowledge.extraction, daily_microusd=0)
        return estimate

    monkeypatch.setattr(provider, "estimate", revoke)
    await capture.capture([source()])
    assert await worker.tick() == ["retry"]
    assert provider.calls == []
    assert (await job_state(db, store))["error_code"] == "extraction.budget_disabled"


async def test_corrupt_input_receipt_digest_is_rejected_before_provider_access(setup):
    db, _, _, store, capture, provider, worker = setup
    await capture.capture([source()])
    async with db.immediate() as conn:
        await conn.execute(update(store.jobs).values(source_sha256="b" * 64))
    assert await worker.tick() == ["quarantined"]
    assert provider.calls == [] and provider.estimates == []
    assert (await job_state(db, store))["error_code"] == "extraction.source_unavailable"


async def test_five_provider_failures_persist_circuit_across_restart_and_day_rollover(setup):
    db, config, now, store, capture, provider, worker = setup
    provider.failure = RuntimeError("provider failure")
    for ordinal in range(5):
        await capture.capture([source(source_identity=f"event:{ordinal}", event_id=ordinal)])
        assert await worker.tick() == ["quarantined"]
    assert len(provider.calls) == 5
    assert (await generation_status(db))["budgets"][0]["circuit_open"]
    now[0] += timedelta(days=1)
    provider.failure = None
    restarted = ExtractionWorker(db, lambda: config, store=store, registry=worker.registry)
    await capture.capture([source(source_identity="event:6", event_id=6)])
    assert await restarted.tick() == ["retry"]
    assert len(provider.calls) == 5


async def test_overage_is_reported_and_blocks_later_calls_instead_of_capping_usage(setup):
    db, _, _, _, capture, provider, worker = setup
    provider.result["actual_microusd"] = 80
    provider.result["actual_tokens"] = 1200
    await capture.capture([source()])
    assert await worker.tick() == ["succeeded"]
    budget = (await generation_status(db))["budgets"][0]
    assert budget["spent_microusd"] == 80 and budget["spent_tokens"] == 1200
    assert budget["circuit_open"]
    await capture.capture([source(source_identity="event:2", event_id=2)])
    assert await worker.tick() == ["retry"]
    assert len(provider.calls) == 1


@pytest.mark.parametrize("content", [object(), "x" * 262145])
async def test_unretainable_output_keeps_known_charges_and_never_repeats_call(setup, content):
    db, _, _, store, capture, provider, worker = setup
    provider.result["outputs"][0]["body"] = content
    await capture.capture([source()])
    assert await worker.tick() == ["quarantined"]
    assert (await job_state(db, store))["error_code"] == "extraction.invalid_output"
    status = await generation_status(db)
    assert status["unknown_calls"] == 0
    assert status["budgets"][0]["spent_microusd"] == 10
    assert status["budgets"][0]["reserved_microusd"] == 0
    assert status["budgets"][0]["consecutive_failures"] == 1
    assert await worker.tick() == [] and len(provider.calls) == 1


async def test_cancelled_paid_call_is_quarantined_before_loop_shutdown(setup, monkeypatch):
    db, _, _, store, capture, provider, worker = setup
    entered, waiting = asyncio.Event(), asyncio.Event()

    async def blocked(request):
        provider.calls.append(request)
        entered.set()
        await waiting.wait()

    monkeypatch.setattr(provider, "generate", blocked)
    await capture.capture([source()])
    running = asyncio.create_task(worker.tick())
    await asyncio.wait_for(entered.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert (await job_state(db, store))["state"] == "quarantined"
    assert (await generation_status(db))["unknown_calls"] == 1
    assert await worker.tick() == [] and len(provider.calls) == 1


async def test_competing_consumers_never_overlap_calls_within_a_project(setup, monkeypatch):
    db, config, _, store, capture, provider, worker = setup
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def blocked(request):
        seen.append(request.scope_key)
        if len(seen) == 2:
            entered.set()
        await release.wait()
        return copy.deepcopy(provider.result)

    monkeypatch.setattr(provider, "generate", blocked)
    worker.timeout = 15
    for project in ("p", "q"):
        for ordinal in range(2):
            await capture.capture(
                [
                    source(
                        scope_key=f"project:{project}",
                        source_identity=f"event:{ordinal}",
                        event_id=ordinal,
                    )
                ]
            )
    competing = ExtractionWorker(db, config, store=store, registry=worker.registry)
    running = asyncio.create_task(worker.tick())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert len(seen) == 2 and set(seen) == {"project:p", "project:q"}
        assert await asyncio.wait_for(competing.tick(), 5) == []
        release.set()
        assert await running == ["succeeded", "succeeded"]
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_injection_of_verification_or_authority_is_quarantined_without_knowledge_writes(
    setup,
):
    db, _, _, _, capture, provider, worker = setup
    provider.result["outputs"][0].update(verification="verified", authority=True)
    await capture.capture([source()])
    assert await worker.tick() == ["quarantined"]
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_records)) == 0
        assert await conn.scalar(select(func.count()).select_from(knowledge_proposals)) == 0
    assert (await generation_status(db))["budgets"][0]["spent_microusd"] == 10


async def test_one_bad_output_rolls_back_all_proposals_but_retains_paid_usage(setup):
    db, _, _, store, capture, provider, worker = setup
    provider.result["outputs"].append(dict(title="Disallowed", body="text", category="illegal"))
    await capture.capture([source()])
    assert await worker.tick() == ["quarantined"]
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(knowledge_proposals)) == 0
    status = await generation_status(db)
    assert status["budgets"][0]["spent_microusd"] == 10
    assert (await job_state(db, store))["result_artifact_id"]


async def test_consolidation_uses_exact_base_and_independent_budget_with_original_evidence(setup):
    db, config, _, store, capture, provider, worker = setup
    service = KnowledgeService(db, config.knowledge)
    original = await service.create(
        snapshot=snapshot(), principal=TRUSTED_LOCAL, project_id="p", idempotency_key="original"
    )
    assert await capture.reconcile_consolidation("p") == 1
    provider.result["outputs"][0]["category"] = "procedure"
    assert await worker.tick() == ["succeeded"]
    shown = await service.show(
        identity=f"record:{original['record_id']}", principal=TRUSTED_LOCAL, project_id="p"
    )
    assert shown["revision_id"] == original["revision_id"]
    async with db.immediate() as conn:
        proposal = (await conn.execute(select(knowledge_proposals))).mappings().one()
        assert str(proposal["record_id"]) == original["record_id"]
        assert str(proposal["base_revision_id"]) == original["revision_id"]
        assert proposal["proposed_snapshot"]["verification"] == "unverified"
    await capture.capture([source()])
    assert await worker.tick() == ["succeeded"]
    assert {row["feature"] for row in (await generation_status(db))["budgets"]} == {
        "extraction",
        "consolidation",
    }
    assert await capture.reconcile_consolidation("p") == 0


async def test_reauthorization_after_capture_rejects_changed_consolidation_base(setup):
    db, config, _, store, capture, provider, worker = setup
    service = KnowledgeService(db, config.knowledge)
    original = await service.create(
        snapshot=snapshot(), principal=TRUSTED_LOCAL, project_id="p", idempotency_key="original"
    )
    await capture.reconcile_consolidation("p")
    await service.update(
        identity=f"record:{original['record_id']}",
        principal=TRUSTED_LOCAL,
        project_id="p",
        if_revision=original["revision_id"],
        idempotency_key="edit",
        patch={"title": "Changed"},
    )
    assert await worker.tick() == ["quarantined"]
    assert provider.calls == []


async def test_retired_and_disputed_sources_cannot_starve_eligible_consolidation(setup):
    db, config, _, _, capture, _, _ = setup
    service = KnowledgeService(db, config.knowledge)
    for ordinal in range(3):
        created = await service.create(
            snapshot=snapshot(),
            principal=TRUSTED_LOCAL,
            project_id="p",
            idempotency_key=f"retired:{ordinal}",
        )
        await service.retire(
            identity=f"record:{created['record_id']}",
            principal=TRUSTED_LOCAL,
            project_id="p",
            if_revision=created["revision_id"],
            idempotency_key=f"retire:{ordinal}",
            reason="test",
        )
    await service.create(
        snapshot=snapshot(),
        principal=TRUSTED_LOCAL,
        project_id="p",
        idempotency_key="eligible",
    )
    assert await capture.reconcile_consolidation("p") == 1


def test_generation_config_defaults_yaml_and_invalid_allowances(tmp_path):
    from src.config import KnowledgeConfig, load_config

    config = AppConfig()
    for feature in (config.knowledge.extraction, config.knowledge.consolidation):
        assert not feature.enabled and feature.daily_microusd == 0 and feature.daily_tokens == 0
        assert feature.provider_id == "" and feature.allowed_providers == []
    path = tmp_path / "config.yaml"
    path.write_text(
        "messaging_platform: none\ndatabase:\n  url: postgresql://test@localhost/test\n"
        "knowledge:\n  extraction:\n    enabled: true\n    provider_id: fake\n"
        "    allowed_providers: [fake]\n    daily_microusd: 42\n    daily_tokens: 8000\n"
        "    policy_version: synthetic-v1\n",
    )
    loaded = load_config(str(path))
    assert loaded.knowledge.extraction.daily_microusd == 42
    assert loaded.knowledge.extraction.policy_version == "synthetic-v1"
    assert loaded.knowledge.extraction.allowed_providers == ["fake"]
    assert not loaded.knowledge.consolidation.enabled and not loaded.memory.enabled
    invalid = KnowledgeConfig()
    invalid.extraction.daily_microusd = -1
    invalid.consolidation.daily_tokens = True
    invalid.extraction.allowed_providers = ["*"]
    assert {error.field for error in invalid.validate()} >= {
        "extraction.daily_microusd",
        "consolidation.daily_tokens",
        "extraction.allowed_providers",
    }


def test_generation_contracts_and_tools_reject_caller_sources_roles_and_scope():
    from pydantic import ValidationError
    from src.commands.contracts.registry import CONTRACTS
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS

    definitions = {entry["name"]: entry for entry in _ALL_TOOL_DEFINITIONS}
    for name in ("knowledge_generation_tick", "knowledge_generation_status"):
        contract = CONTRACTS.get(name).contract.execution
        assert contract.capability == name and contract.retry_safe
        assert definitions[name]["input_schema"]["additionalProperties"] is False
        with pytest.raises(ValidationError):
            contract.args_model.model_validate(
                {"project_id": "q", "principal": "operator", "sources": ["private log"]}
            )


async def test_disabled_feature_preserves_pending_jobs_and_never_looks_up_provider(
    setup, monkeypatch
):
    db, config, _, store, capture, _, worker = setup
    await capture.capture([source()])
    config.knowledge.extraction.enabled = False
    monkeypatch.setattr(worker.registry, "lookup", lambda _: pytest.fail("provider lookup"))
    assert await worker.tick() == []
    assert (await job_state(db, store))["state"] == "pending"


async def test_command_tick_and_diagnostics_reject_supervisor_and_unrelated_service(setup):
    from src.commands.knowledge_commands import KnowledgeCommandsMixin
    from tests.record_helpers import worker_principal

    db, config, _, _, _, _, _ = setup
    owner = KnowledgeCommandsMixin()
    owner.db, owner.config = db, config
    principal = await worker_principal(
        db, elevated=True, grants=["knowledge_generation_tick", "knowledge_generation_status"]
    )
    for identity in (principal, ExecutionPrincipal.service("unrelated")):
        with principal_context(identity):
            assert not (await owner._cmd_knowledge_generation_tick({}))["success"]
            assert not (await owner._cmd_knowledge_generation_status({}))["success"]
    with principal_context(TRUSTED_LOCAL):
        assert (await owner._cmd_knowledge_generation_status({}))["success"]


async def test_stateless_legacy_port_can_only_propose_and_reports_ambiguity_contract():
    from src.knowledge.extraction import ExtractionRequest

    seen = []

    async def propose(wire):
        seen.append(wire)
        return dict(outputs=[], actual_tokens=0, actual_microusd=0)

    port = MemoryExtractionPort(
        propose,
        lambda wire: dict(tokens=500, microusd=10),
        provider_id="fake",
        provider_version="test:1",
    )
    request = ExtractionRequest("operation", "extraction", "project:p", ())
    assert (await port.generate(request))["outputs"] == []
    assert port.estimate(request)["microusd"] == 10
    assert seen == [request.wire()]
    assert not port.handshake()["authoritative_writes"]
    for method in ("kv_set", "save_document", "promote", "consolidate"):
        assert not hasattr(port, method)


async def test_wake_is_only_a_signal_and_loop_stops_without_command_or_db_when_disabled():
    from src.event_bus import EventBus

    config = AppConfig()
    loop = KnowledgeGenerationLoop(
        lambda: pytest.fail("disabled dispatch"),
        lambda: config,
        EventBus(validate_events=False),
        interval=0.01,
    )
    loop.start()
    loop.wake({"body": "never process ephemeral evidence"})
    await asyncio.sleep(0.02)
    await loop.stop()
    assert loop._task is None and loop._unsubscribers == []


@pytest.mark.migration
async def test_failure_circuit_migration_replay_and_nonempty_rollback_guard(setup):
    db, _, _, store, _, _, _ = setup
    migration = importlib.import_module(
        "migrations.versions.a00000000064_knowledge_failure_circuit"
    )

    def run(sync, action):
        with Operations.context(MigrationContext.configure(sync)):
            getattr(migration, action)()

    async with db.immediate() as conn:
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        await conn.run_sync(lambda sync: run(sync, "downgrade"))
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        await conn.run_sync(lambda sync: run(sync, "upgrade"))
        assert "consecutive_failures" in await conn.run_sync(
            lambda sync: {
                column["name"] for column in inspect(sync).get_columns("knowledge_feature_budgets")
            }
        )
        await store.configure_budget_on(scope_key="project:p", feature="extraction", conn=conn)
    with pytest.raises(RuntimeError, match="read-only rollback"):
        async with db.immediate() as conn:
            await conn.run_sync(lambda sync: run(sync, "downgrade"))
