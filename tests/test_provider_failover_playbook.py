"""The building-block port preserves the recorded provider-failover policy.

Run both artifacts through the real engine and typed command executor. Command
adapters return scripted business outcomes; provider service behavior remains
covered by the existing reroute, availability and escalation suites.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.models import CommandResult
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from tests._private_values_helper import ReadyPrivateValues
from tests.fixtures.contracts.engine_contracts import registry_with
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

ROOT = Path(__file__).parent / "fixtures"
PORT = ROOT / "provider_failover" / "playbook-port"
ORIGINAL = PORT / "original.artifact.json"
CURRENT = ROOT / "playbooks" / "v2" / "provider-failover" / "artifact.json"
SCENARIOS = json.loads((PORT / "scenarios.json").read_text())
COMMANDS = ("provider_reroute", "provider_availability_notify")


def test_original_recording_and_executable_policy_are_preserved():
    # This is the pre-port recording, never regenerated from the new source.
    assert hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == (
        "e4c946b8eddf8fcbac5ed552887091e1eb822dd851a1647f624bb84276724607"
    )
    before, after = [json.loads(path.read_text()) for path in (ORIGINAL, CURRENT)]
    for body in (before, after):
        for rule in body["rules"]:
            rule.pop("source")
        for step in body["steps"].values():
            step.pop("source")
    assert after["rules"] == before["rules"]
    assert after["steps"] == before["steps"]
    assert after["compiled_against"] == before["compiled_against"]
    assert set(after["trusted_execution"]["required_capabilities"]) == set(COMMANDS)
    assert not after["trusted_execution"].get("state_schema_version")


async def _trace(path, scenario):
    definition = load_definition_json(path.read_text())
    contracts = [CONTRACTS.require(name).contract for name in COMMANDS]
    registry, adapter = registry_with(*contracts)
    for name, outcome in zip(COMMANDS, (scenario["reroute"], scenario["notify"])):
        if outcome is None:
            continue
        if outcome == "runtime_error":
            adapter.queue.append(RuntimeError("scenario failure"))
            continue
        value = {"outcome": outcome}
        if name == "provider_availability_notify":
            value.update(provider=scenario["event"]["provider"],
                         generation=scenario["event"]["generation"])
        adapter.queue.append(CommandResult(
            outcome=outcome,
            value=registry.require(name).contract.execution.result_model.model_validate(value),
            summary=outcome,
        ))
    store = InMemoryArtifactStore()
    store.put(definition)
    ref = artifact_ref_for(definition)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry, artifact_store=store,
            private_values=ReadyPrivateValues(), clock=lambda: 1000.0,
        ),
        runs=runs, waits=None, activations=StubActivations([ref]),
    )
    dispatched = await engine.dispatch_event(scenario["event"], TRUSTED_LOCAL)
    assert len(dispatched.run_ids) == 1
    snapshot = runs.snapshots[dispatched.run_ids[0]]
    assert snapshot.lifecycle.value == scenario["terminal"]
    expected = ["provider_reroute"]
    if scenario["notify"] is not None:
        expected.append("provider_availability_notify")
    assert adapter.names == expected  # a refusal never retries or calls the next step
    assert not adapter.queue
    # A replay of the same event reuses the run and never invokes a command again.
    replay = await engine.dispatch_event(scenario["event"], TRUSTED_LOCAL)
    assert replay.run_ids == dispatched.run_ids
    assert adapter.names == expected
    calls = [(name, args.model_dump(exclude_unset=True)) for name, args, _ in adapter.calls]
    assert calls[0] == ("provider_reroute", {})
    if scenario["notify"] is not None:
        assert calls[1] == ("provider_availability_notify", {
            "provider": scenario["event"]["provider"],
            "generation": scenario["event"]["generation"],
        })
    return calls, [
        (r.step_id, r.receipt_kind, r.outcome) for r in runs.receipts
    ], snapshot.bindings


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case["id"])
async def test_original_and_ported_event_traces_match(scenario):
    assert await _trace(ORIGINAL, scenario) == await _trace(CURRENT, scenario)
