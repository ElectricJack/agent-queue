"""Preservation evidence for the reusable-building-block audit (fresh-horizon-48).

The supervisor supplied the active project artifact for fresh-horizon-48. T3 adds
an opt-in policy contract while retaining that historical evidence. Its only step
acknowledges a readiness hint; the Git
train consumes its literal policy independently. Adding a loop, command, state
namespace or script would introduce behavior instead of replacing any logic.

The installed source text is unavailable. Compare the recorded artifact, never
reconstruct that source or claim its text matches the shipped template. Scope,
source hash and compilation time differ; executable policy and graph do not.
These tests exercise the engine with in-memory stores, not live activation or
the Git train's publication/fencing mechanisms (covered by the train suites).
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.definition import canonical_bytes, load_definition_json, source_digest
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.train_policy import TrainBisectionPolicy, TrainPackingPolicy, TrainPolicy
from tests.fixtures.contracts.engine_contracts import registry_with
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingBus,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "tests/fixtures/playbooks/scenarios/fast-smooth-train"
BUNDLE = ROOT / "src/prompts/reviewed_playbooks/fast-smooth-train"
EVIDENCE = json.loads((SCENARIOS / "equivalence.json").read_text())
ARTIFACTS = (SCENARIOS / "installed.artifact.json", BUNDLE / "artifact.json")


def test_recorded_identity_and_policy_are_preserved():
    provenance = EVIDENCE["provenance"]
    for path, key in (
        (ARTIFACTS[0], "installed_artifact_sha256"),
        (SCENARIOS / "legacy-shipped.artifact.json", "shipped_artifact_sha256"),
        (SCENARIOS / "legacy-shipped.source.md", "shipped_source_bytes_sha256"),
    ):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance[key]
    assert source_digest((SCENARIOS / "legacy-shipped.source.md").read_text()) == (
        "sha256:" + provenance["shipped_source_sha256"]
    )
    assert (ROOT / "src/prompts/integration_playbooks/fast-smooth-train.md").read_bytes() == (
        BUNDLE / "source.md"
    ).read_bytes()

    installed, shipped = [json.loads(path.read_text()) for path in (
        ARTIFACTS[0], SCENARIOS / "legacy-shipped.artifact.json",
    )]
    differences = {key for key in installed.keys() | shipped.keys()
                   if installed.get(key) != shipped.get(key)}
    assert differences == set(provenance["metadata_differences"])
    assert installed["scope"] == {"type": "project", "project_id": "agent-queue"}
    assert shipped["scope"] == {"type": "system"}
    assert installed["integration_policy"] == shipped["integration_policy"] == (
        EVIDENCE["integration_policy"]
    )
    assert installed["compiled_against"] == {"commands": {}, "profiles": {}}
    assert set(installed["steps"]) == {"completed"}
    assert installed["steps"]["completed"]["type"] == "terminal"


def test_legacy_artifacts_keep_canonical_identity_and_disabled_features():
    for path in (ARTIFACTS[0], SCENARIOS / "legacy-shipped.artifact.json"):
        definition = load_definition_json(path.read_text())
        assert canonical_bytes(definition) == path.read_bytes()
        policy = definition.integration_policy.train
        assert policy.packing is None
        assert policy.bisection is None
        assert policy.parallel_disjoint is False


def test_shipped_policy_explicitly_records_disabled_bounded_contract():
    definition = load_definition_json(ARTIFACTS[1].read_text())
    policy = definition.integration_policy.train
    assert policy.packing == TrainPackingPolicy()
    assert policy.bisection == TrainBisectionPolicy()
    assert policy.parallel_disjoint is False
    assert not policy.packing.enabled and not policy.bisection.enabled
    # Rebuilding still leaves the graph a terminal-only readiness observer.
    assert definition.compiled_against.commands == {}
    assert set(definition.steps) == {"completed"}
    assert definition.steps["completed"].type == "terminal"


@pytest.mark.parametrize("case", EVIDENCE["policy_actions"], ids=lambda case: case["state"])
def test_installed_and_shipped_train_decisions_match(case):
    for path in ARTIFACTS:
        definition = load_definition_json(path.read_text())
        policy = TrainPolicy.model_validate(
            definition.integration_policy.model_dump(mode="json")["train"]
        )
        assert policy.action(case["state"]) == case["action"]
        assert policy.wait_seconds == 30
        assert policy.required_checks is None


@pytest.mark.parametrize("case", EVIDENCE["scenarios"], ids=lambda case: case["id"])
async def test_installed_and_shipped_graph_outcomes_match(case):
    observations = []
    for path in ARTIFACTS:
        definition = load_definition_json(path.read_text())
        store = InMemoryArtifactStore()
        store.put(definition)
        runs = RecordingRunRepository()
        bus = RecordingBus()
        contracts, adapter = registry_with()
        engine = PlaybookEngine(
            services=EngineServices(
                contracts=contracts, clock=lambda: 1_000.0, artifact_store=store,
                bus=bus, private_values=AsyncMock(),
            ),
            runs=runs,
            waits=None,
            activations=StubActivations([artifact_ref_for(definition)]),
        )
        result = await engine.dispatch_event(case["event"], TRUSTED_LOCAL)
        outcomes = [runs.snapshots[run_id].lifecycle.value for run_id in result.run_ids]
        assert list(result.rules_selected) == case["rules_selected"]
        assert outcomes == case["outcomes"]
        assert result.pending == ()
        assert result.commands == ()
        assert adapter.calls == []
        receipts = [(r.step_id, r.receipt_kind, r.outcome) for r in runs.receipts]
        assert receipts == [("completed", "step", "success")] * len(outcomes)
        observations.append((result.rules_selected, outcomes, receipts))

        if case["replay"]:
            before = (len(runs.snapshots), len(runs.receipts), len(bus.events))
            replay = await engine.dispatch_event(case["event"], TRUSTED_LOCAL)
            assert replay.run_ids == result.run_ids
            assert replay.deduplicated == result.rules_selected
            assert (len(runs.snapshots), len(runs.receipts), len(bus.events)) == before
            assert adapter.calls == []
    assert observations[0] == observations[1]
