"""Package 7 §11.2 — performance checks at production-like graph sizes.

The corpus is ``tests/fixtures/playbooks/cutover/perf-corpus/``: the shipped
``default-pipeline`` artifact (the largest enabled artifact today) and a
synthetic upper bound at 5x its rule count and 10x its node count — 25 rules
and 170 nodes, built by ``generate.py`` in that directory.  Measure 12's
300 ms gate is asserted against the synthetic case so it cannot quietly weaken
as the shipped pipeline shrinks.

Two kinds of budget, following the rest of this package: the statement-count
budget for the rollback window's evidence reads is deterministic and always
runs; every wall-clock budget takes ``perf_strict`` and only runs on a quiet
box (``AQ_PERF_STRICT=1``).
"""

from __future__ import annotations

import pathlib
import time
import uuid

import pytest

from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.cutover import _p95 as p95
from src.playbooks.cutover_window import GRAPH_LATENCY_GATE_MS, PENDING_EVENT_REASONS
from src.playbooks.definition import PlaybookDefinition, canonical_bytes, load_definition_json
from src.playbooks.receipts import StepReceipt
from src.playbooks.run_state import RunSnapshot
from tests.perf.test_hierarchy_statements import count_statements

pytestmark = pytest.mark.perf

CORPUS = pathlib.Path("tests/fixtures/playbooks/cutover/perf-corpus")
SYNTHETIC = CORPUS / "synthetic-25x170.artifact.json"
SHIPPED = CORPUS / "default-pipeline.artifact.json"

#: §11.2: 5x the default pipeline's 5 rules, 10x its 17 nodes.
SYNTHETIC_RULES = 25
SYNTHETIC_NODES = 170

#: How many probes measure 12's p95 is taken over, matching the command.
GRAPH_SAMPLES = 5

#: The window status makes exactly this many evidence statements against the
#: V2 tables, however many runs and receipts the window holds: one aggregate
#: per source and no per-run reads.
WINDOW_EVIDENCE_STATEMENTS = 7

#: Wall-clock budget for storing and re-loading (hash-verifying) the synthetic
#: artifact — "graph load" in the roadmap's list.
ARTIFACT_LOAD_BUDGET_MS = 300.0

#: Wall-clock budget for one receipt boundary write at this graph size.
RECEIPT_WRITE_P95_BUDGET_MS = 50.0

NOW = 1_700_000_000.0


def _synthetic() -> PlaybookDefinition:
    return load_definition_json(SYNTHETIC.read_text(encoding="utf-8"))


def _shipped() -> PlaybookDefinition:
    return load_definition_json(SHIPPED.read_text(encoding="utf-8"))


def _ref(definition: PlaybookDefinition, store: ArtifactStore):
    return store.put(
        definition,
        source_digest=definition.source_hash,
        contract_fingerprint=definition.contract_fingerprint(),
        profile_fingerprint="",
        compiler_build=definition.compiler_build or "perf-corpus",
        version=definition.version,
    )


# ---------------------------------------------------------------------------
# The corpus itself
# ---------------------------------------------------------------------------


def test_synthetic_corpus_is_the_locked_upper_bound():
    definition = _synthetic()
    assert len(definition.rules) == SYNTHETIC_RULES
    assert len(definition.steps) == SYNTHETIC_NODES
    # Canonical bytes: a regeneration with no generator change is a no-op.
    assert SYNTHETIC.read_bytes() == canonical_bytes(definition)


def test_shipped_corpus_is_the_default_pipeline_the_bound_is_derived_from():
    """§11.2 derived the bound from 5 rules / 17 nodes; the pipeline has since
    grown to 19 nodes.  The bound stays locked at 25 / 170 — it must remain
    an upper bound on the shipped artifact, which is what this pins."""
    definition = _shipped()
    assert definition.id == "default-pipeline"
    assert len(definition.rules) * 5 <= SYNTHETIC_RULES
    assert len(definition.steps) * 5 <= SYNTHETIC_NODES


# ---------------------------------------------------------------------------
# Measure 12 — graph projection latency
# ---------------------------------------------------------------------------


def _project(definition: PlaybookDefinition, tmp_path) -> list[float]:
    from src.commands.contracts import CONTRACTS
    from src.playbooks.graph_projection import project_graph

    store = ArtifactStore(str(tmp_path))
    ref = _ref(definition, store)
    samples = []
    for _ in range(GRAPH_SAMPLES):
        started = time.perf_counter()
        response = project_graph(definition, ref, None, contracts=CONTRACTS)
        samples.append((time.perf_counter() - started) * 1000.0)
        assert len(response["nodes"]) == len(definition.steps)
    return sorted(samples)


@pytest.mark.parametrize("corpus", ["synthetic", "shipped"])
def test_graph_projection_p95_meets_measure_12_gate(perf_strict, tmp_path, corpus):
    definition = _synthetic() if corpus == "synthetic" else _shipped()
    samples = _project(definition, tmp_path)
    assert p95(samples) <= GRAPH_LATENCY_GATE_MS, samples


def test_graph_projection_of_the_synthetic_corpus_is_complete(tmp_path):
    """Always runs: the gate is only meaningful if the projection is whole."""
    from src.commands.contracts import CONTRACTS
    from src.playbooks.graph_projection import project_graph

    definition = _synthetic()
    store = ArtifactStore(str(tmp_path))
    response = project_graph(definition, _ref(definition, store), None, contracts=CONTRACTS)
    assert len(response["rules"]) == SYNTHETIC_RULES
    assert len(response["nodes"]) == SYNTHETIC_NODES


# ---------------------------------------------------------------------------
# Graph load — store and hash-verified reload
# ---------------------------------------------------------------------------


def test_artifact_store_round_trip_at_the_upper_bound(perf_strict, tmp_path):
    definition = _synthetic()
    store = ArtifactStore(str(tmp_path))
    started = time.perf_counter()
    ref = _ref(definition, store)
    loaded = store.load(ref.artifact_sha256)
    elapsed = (time.perf_counter() - started) * 1000.0
    assert loaded.artifact_sha256() == ref.artifact_sha256
    assert elapsed <= ARTIFACT_LOAD_BUDGET_MS, elapsed


# ---------------------------------------------------------------------------
# Receipt writes and the window's evidence reads, on a seeded database
# ---------------------------------------------------------------------------


async def _seed_runs(any_db, definition: PlaybookDefinition, tmp_path, *, runs: int) -> list[float]:
    from sqlalchemy import insert

    from src.database.tables import playbook_artifacts

    store = ArtifactStore(str(tmp_path))
    ref = _ref(definition, store)
    async with any_db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=ref.artifact_sha256,
                playbook_id=definition.id,
                scope="system",
                scope_identifier="",
                schema_generation=ref.schema_generation,
                version=ref.version,
                source_digest=ref.source_digest,
                contract_fingerprint=ref.contract_fingerprint,
                profile_fingerprint="",
                compiler_build=ref.compiler_build,
                path=store.path_for(ref.artifact_sha256),
                size_bytes=len(canonical_bytes(definition)),
                validation="{}",
                compiled_at=None,
                created_at=NOW,
            )
        )
    write_ms: list[float] = []
    for index in range(runs):
        rule = definition.rules[index % len(definition.rules)]
        started_at = NOW + index
        snapshot = await any_db.create_run(
            RunSnapshot(
                run_id=uuid.uuid4().hex,
                playbook_id=definition.id,
                artifact_sha256=ref.artifact_sha256,
                rule_id=rule.id,
                event={"event_id": f"e{index}", "_received_at": started_at - 0.25},
                event_type=rule.trigger.event_type,
                event_id=f"e{index}",
                dispatch_id=f"d{index}",
                current_step_id=rule.entry_step,
                started_at=started_at,
                updated_at=started_at,
            )
        )
        receipt = StepReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=snapshot.run_id,
            artifact_sha256=snapshot.artifact_sha256,
            rule_id=snapshot.rule_id,
            step_id=rule.entry_step,
            step_kind="command",
            outcome="success",
            started_at=started_at,
            snapshot_version=snapshot.version + 1,
            completed_at=started_at + 0.5,
        )
        started = time.perf_counter()
        await any_db.commit_boundary(snapshot, receipt)
        write_ms.append((time.perf_counter() - started) * 1000.0)
    return sorted(write_ms)


async def _window_reads(any_db, since: float) -> None:
    await any_db.count_v2_runs_by_playbook(since)
    await any_db.v2_dispatch_latencies_ms(since)
    await any_db.wait_resume_latencies_ms(since)
    await any_db.count_step_receipts_since(since)
    await any_db.agent_task_wait_orphans(NOW + 10_000)
    await any_db.agent_task_cancellations_since(since)
    await any_db.pending_event_summary(reasons=PENDING_EVENT_REASONS)


async def test_window_evidence_reads_are_a_fixed_number_of_statements(any_db, tmp_path):
    """Deterministic: no per-run read hides in the window's evidence path."""
    definition = _synthetic()
    await _seed_runs(any_db, definition, tmp_path, runs=200)

    async with count_statements(any_db) as counter:
        await _window_reads(any_db, since=NOW - 1)

    assert counter["n"] == WINDOW_EVIDENCE_STATEMENTS, counter


async def test_window_evidence_reads_scale_flat_with_run_volume(any_db, tmp_path):
    """The count at 200 runs is the count at 20: an aggregate, not a scan-and-loop."""
    definition = _synthetic()
    await _seed_runs(any_db, definition, tmp_path, runs=20)
    async with count_statements(any_db) as small:
        await _window_reads(any_db, since=NOW - 1)
    await _seed_runs(any_db, _shipped(), tmp_path, runs=180)
    async with count_statements(any_db) as large:
        await _window_reads(any_db, since=NOW - 1)

    assert small["n"] == large["n"] == WINDOW_EVIDENCE_STATEMENTS
    counts = await any_db.count_v2_runs_by_playbook(NOW - 1)
    assert sum(counts.values()) == 200


async def test_receipt_write_p95_at_the_upper_bound(perf_strict, any_db, tmp_path):
    write_ms = await _seed_runs(any_db, _synthetic(), tmp_path, runs=100)
    assert p95(write_ms) <= RECEIPT_WRITE_P95_BUDGET_MS, write_ms[-5:]
