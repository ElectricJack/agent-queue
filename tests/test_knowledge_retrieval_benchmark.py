"""The G5 pre-gate comparison: lexical vs offline semantic over one corpus.

Every arm is measured through the ordinary core paths against real PostgreSQL
records. The semantic arm's chunks come from the hydration and index-receipt
path, so its reach is exactly what core acknowledged. Nothing here activates a
provider, pays for generation or satisfies G5: the report names the evidence the
gate still needs.
"""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import insert

from src.commands.principal import TRUSTED_LOCAL
from src.config import MemoryConfig
from src.database.tables import record_source_artifacts
from src.knowledge.benchmark import (
    ARMS,
    DECLARED_METRICS,
    G5_MISSING_EVIDENCE,
    SCENARIOS,
    Corpus,
    OfflineTermIndex,
    compare_arms,
    index_corpus,
    retrieval_boundary,
)
from src.knowledge.index_receipts import DerivedIndexReceipts
from src.knowledge.registration import RetrievalProviderRegistry
from src.knowledge.service import KnowledgeService
from tests.record_helpers import knowledge_config, seed_project, snapshot, worker_principal

LOCAL = {"principal": TRUSTED_LOCAL, "project_id": "p"}
TOP_K = 8

CORPUS = {
    "current_procedure": {
        "title": "Deploy to the staging rail",
        "summary": "Run the health gate, then freeze the schema and release.",
        "body": "Deploy to the staging rail: run the health gate, then freeze the schema.",
    },
    "superseded_procedure": {
        "title": "Deploy to the staging rail",
        "summary": "Skip the health gate when in a hurry.",
        "body": "Deploy to the staging rail: skip the health gate when in a hurry.",
    },
    "incident_evidence": {
        "title": "Azure vault 92 retained log",
        "summary": "Observed rotation failure at 03:12 UTC.",
        "body": "Azure vault 92 rotation failed at 03:12 UTC with a rejected certificate.",
    },
    "prior_summary": {
        "title": "Azure vault 92 prior agent summary",
        "summary": "Prior agent assertion about the same incident.",
        "body": "Azure vault 92 was probably caused by an expired certificate, per an agent note.",
    },
    "current_fact": {
        "title": "Milvus retention",
        "summary": "Knowledge chunks are retained for 30 days.",
        "body": "The Milvus collection retention for knowledge chunks is 30 days by default.",
    },
    "retired_fact": {
        "title": "Milvus retention (retired)",
        "summary": "Older advice, no longer current.",
        "body": "The Milvus collection retention for knowledge chunks is 90 days by default.",
    },
    "current_claim": {
        "title": "Provider failover ordering",
        "summary": "Failover prefers the configured preference order.",
        "body": "Provider failover ordering follows the configured pool preference order.",
    },
    "disputed_claim": {
        "title": "Provider failover ordering (disputed)",
        "summary": "Conflicting claim, not verified.",
        "body": "Provider failover ordering always prefers the cheapest model first.",
    },
}
PRIVATE = {
    "title": "Cross project rotation key",
    "summary": "Confidential.",
    "body": "Cross project confidential rotation key material for project q only.",
}


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    await seed_project(db, "q")
    config = knowledge_config(global_enabled=True)
    config.semantic.enabled = True
    return KnowledgeService(db, config)


async def retained_evidence(service):
    from uuid import uuid4

    artifact_id = uuid4()
    async with service.db.immediate() as conn:
        await conn.execute(
            insert(record_source_artifacts).values(
                artifact_id=artifact_id,
                scope_key="project:p",
                content_sha256="a" * 64,
                byte_size=5,
                media_type="text/plain",
                storage_key=f"record-artifacts/{artifact_id}",
            )
        )
    return [{"source_id": "observed", "kind": "artifact", "artifact_id": str(artifact_id),
                 "sha256": "a" * 64}]


async def build_corpus(service):
    """Create the labeled corpus, plus the ineligible and private distractors."""

    corpus = Corpus()
    entries = []
    for label, doc in CORPUS.items():
        record = await service.create(
            snapshot=snapshot(**doc), idempotency_key=label, **LOCAL
        )
        corpus.revisions[label] = record
        entries.append((label, record))
    corpus.revisions["private_record"] = await service.create(
        snapshot=snapshot(**PRIVATE),
        idempotency_key="private",
        principal=TRUSTED_LOCAL,
        project_id="q",
    )
    corpus.ineligible = {"superseded_procedure", "retired_fact", "disputed_claim"}
    corpus.labels = tuple(CORPUS)
    await service.retire(
        identity=f"record:{corpus.revisions['superseded_procedure']['record_id']}",
        reason="Superseded by the current rail procedure",
        if_revision=corpus.revisions["superseded_procedure"]["revision_id"],
        idempotency_key="retire",
        **LOCAL,
    )
    await service.retire(
        identity=f"record:{corpus.revisions['retired_fact']['record_id']}",
        reason="Retention advice changed",
        if_revision=corpus.revisions["retired_fact"]["revision_id"],
        idempotency_key="retire-fact",
        **LOCAL,
    )
    await service.verify(
        identity=f"record:{corpus.revisions['disputed_claim']['record_id']}",
        verification="disputed",
        reason="Conflicting observations",
        evidence=await retained_evidence(service),
        if_revision=corpus.revisions["disputed_claim"]["revision_id"],
        idempotency_key="dispute",
        **LOCAL,
    )
    return corpus, entries


async def report_for(service, corpus, entries, *, principal=None, project_id="p"):
    provider = OfflineTermIndex()
    receipts = DerivedIndexReceipts(
        service,
        RetrievalProviderRegistry(lambda name: provider),
        MemoryConfig(enabled=True),
    )
    acknowledged = await index_corpus(
        receipts, provider, entries, principal=principal or TRUSTED_LOCAL, project_id=project_id
    )
    boundary = retrieval_boundary(service, provider)

    async def lexical(**kwargs):
        return await service.search(
            principal=principal or TRUSTED_LOCAL,
            project_id=project_id,
            **kwargs,
        )

    report = await compare_arms(
        corpus,
        lexical,
        boundary,
        principal=principal or TRUSTED_LOCAL,
        project_id=project_id,
    )
    report["acknowledged_revisions"] = len(acknowledged)
    return report, provider


async def test_the_offline_comparison_reports_every_declared_metric(service):
    corpus, entries = await build_corpus(service)
    report, _ = await report_for(service, corpus, entries)
    assert set(report["arms"]) == set(ARMS)
    assert report["declared_metrics"] == list(DECLARED_METRICS)
    for arm in ARMS:
        assert set(DECLARED_METRICS) <= set(report["arms"][arm])
    assert report["acknowledged_revisions"] == len(CORPUS)
    assert report["corpus"]["records"] == len(CORPUS)
    assert report["corpus"]["ineligible"] == sorted(corpus.ineligible)
    assert report["measurement"] == "offline_contract"
    assert report["model_quality"] == "unmeasured"


async def test_neither_arm_serves_an_ineligible_or_private_record(service):
    corpus, entries = await build_corpus(service)
    report, _ = await report_for(service, corpus, entries)
    assert report["protected_failures"] == []
    assert report["comparisons"]["zero_protected_failures"] is True
    for arm in ("lexical", "semantic"):
        assert report["arms"][arm]["ineligible_selection_rate"] == 0.0
        assert report["arms"][arm]["exact_citation_rate"] == 1.0
        assert report["arms"][arm]["duplicate_rate"] == 0.0


async def test_the_semantic_arm_is_never_worse_than_lexical_on_the_oracle(service):
    corpus, entries = await build_corpus(service)
    report, _ = await report_for(service, corpus, entries)
    # Only the irrelevant query has no provider hit at all, so exactly one
    # semantic request answers from a fresh lexical read.
    assert report["arms"]["semantic"]["lexical_fallbacks"] == 1
    lexical, semantic = report["arms"]["lexical"], report["arms"]["semantic"]
    assert report["comparisons"] == {
        "required_evidence_recall_not_lower": True,
        "precision_at_8_not_lower": True,
        "zero_protected_failures": True,
    }
    assert semantic["required_evidence_recall"] >= lexical["required_evidence_recall"]
    assert semantic["empty_bundle_accuracy"] == 1.0
    assert report["arms"]["no_memory"]["selected_records"] == 0
    assert report["arms"]["no_memory"]["required_evidence_recall"] == 0.0


async def test_an_empty_index_falls_back_to_a_fresh_lexical_read(service):
    corpus, _entries = await build_corpus(service)
    report, provider = await report_for(service, corpus, [])
    assert report["acknowledged_revisions"] == 0
    assert provider.chunks == {}
    assert report["arms"]["semantic"]["lexical_fallbacks"] == len(SCENARIOS)
    assert report["arms"]["semantic"]["selected_records"] == report["arms"]["lexical"][
        "selected_records"
    ]
    assert report["arms"]["lexical"]["selected_records"] > 0
    assert report["protected_failures"] == []


async def test_an_unacknowledged_revision_is_not_in_the_semantic_index(service):
    from src.knowledge.index_receipts import index_state_for_provider

    corpus, entries = await build_corpus(service)
    withheld = [entry for entry in entries if entry[0] != "current_procedure"]
    report, provider = await report_for(service, corpus, withheld)
    receipted = {
        str(row["record_id"]) for row in await index_state_for_provider(
            service.db, provider.provider_id
        )
    }
    assert corpus.revisions["current_procedure"]["record_id"] not in receipted
    assert not any(
        key[0] == corpus.revisions["current_procedure"]["record_id"]
        for key in provider.chunks
    )
    # The lexical path still answers for an unindexed revision, and the two
    # scenarios the offline index cannot serve fall back instead of failing.
    assert report["arms"]["lexical"]["required_evidence_recall"] > 0.0
    assert report["arms"]["semantic"]["lexical_fallbacks"] == 2
    assert report["protected_failures"] == []


async def test_a_worker_principal_measures_only_its_authorized_corpus(service):
    corpus, entries = await build_corpus(service)
    worker = await worker_principal(service.db)
    report, _ = await report_for(service, corpus, entries, principal=worker, project_id="p")
    assert report["acknowledged_revisions"] == len(CORPUS)
    assert report["protected_failures"] == []
    for arm in ("lexical", "semantic"):
        assert report["arms"][arm]["ineligible_selection_rate"] == 0.0
        assert report["arms"][arm]["exact_citation_rate"] == 1.0


async def test_the_report_records_that_g5_is_not_satisfied_and_why(service, tmp_path):
    corpus, entries = await build_corpus(service)
    report, _ = await report_for(service, corpus, entries)
    assert report["g5_satisfied"] is False
    assert report["g5_missing_evidence"] == list(G5_MISSING_EVIDENCE)
    assert any("real installed provider" in item for item in report["g5_missing_evidence"])
    # Repeatable operator evidence: AQ_RETRIEVAL_BENCHMARK_OUT=<path> keeps the
    # exact report this gate decision would read.
    path = tmp_path / "retrieval-arms.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["g5_satisfied"] is False
    saved = os.environ.get("AQ_RETRIEVAL_BENCHMARK_OUT")
    if saved:
        Path(saved).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def test_every_scenario_declares_an_oracle_and_a_protected_verdict():
    for scenario in SCENARIOS:
        assert scenario.scenario_id and scenario.query
        assert scenario.protected is True
        assert not (set(scenario.required) & set(scenario.forbidden))
        assert scenario.expects_empty is False or not scenario.required
