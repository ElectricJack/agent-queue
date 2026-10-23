"""Compare the building-block declaration with the captured shipped pipeline.

Only the command handler's responses are substituted. Real dispatch, guards,
expression resolution, typed adapters, transitions and receipts execute for
both artifacts. Database effects and ingest receipt reuse are covered by the
unchanged test_default_pipeline_intake.py suite.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.contracts.registry import ContractRegistry
from src.commands.principal import ExecutionPrincipal
from src.playbooks.definition import canonical_bytes, load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

FIXTURES = Path("tests/fixtures/playbooks/scenarios/default-pipeline")
SCENARIOS = json.loads((FIXTURES / "scenarios.json").read_text())
BEFORE = FIXTURES / "before.artifact.json"
AFTER = Path("tests/fixtures/playbooks/v2/default-pipeline/artifact.json")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_captured_baseline_and_executable_graph_are_unchanged():
    before = load_definition_json(BEFORE.read_text())
    after = load_definition_json(AFTER.read_text())
    assert "sha256:" + hashlib.sha256(BEFORE.read_bytes()).hexdigest() == (
        SCENARIOS["baseline_artifact_sha256"]
    )
    assert canonical_bytes(before) == BEFORE.read_bytes()
    assert before.source_hash == SCENARIOS["baseline_source_sha256"]

    def executable(artifact):
        body = json.loads(canonical_bytes(artifact))
        # Compilation provenance changes with the source; executable fields do not.
        for field in ("source_hash", "compiled_at", "trusted_execution"):
            body.pop(field, None)
        for item in [*body["rules"], *body["steps"].values()]:
            item.pop("source")
        return _json_bytes(body)

    assert executable(after) == executable(before)
    assert set(after.trusted_execution.required_capabilities) == {
        "ensure_spec_ingest", "gate_create", "task_batch_commit",
    }
    assert not after.required_features
    assert not after.trusted_execution.scripts
    assert after.trusted_execution.state_schema_version is None


async def _execute(path, scenario):
    artifact = load_definition_json(path.read_text())
    calls = []

    async def execute(command, inputs):
        calls.append({"command": command, "inputs": inputs})
        if scenario.get("raises"):
            raise RuntimeError("scenario command failure")
        return scenario["response"]

    handler = SimpleNamespace(execute=execute)
    # Pin just the real registrations this artifact uses; unrelated generated
    # command discovery is not part of these execution scenarios.
    contracts = ContractRegistry()
    for name in artifact.compiled_against.commands:
        contracts.register(CONTRACTS.require(name))
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=contracts,
            clock=lambda: 4.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([artifact_ref_for(artifact)]),
    )
    set_handler_provider(lambda: handler)
    try:
        event = {"event_id": scenario["name"], **scenario["event"]}
        result = await engine.dispatch_event(
            event, ExecutionPrincipal.service("playbook-dispatch"),
        )
        if scenario.get("repeat_event"):
            count = len(calls)
            await engine.dispatch_event(event, ExecutionPrincipal.service("playbook-dispatch"))
            assert len(calls) == count  # A repeated delivery must not repeat the effect.
    finally:
        set_handler_provider(None)
    return {
        "calls": calls,
        "rules": list(result.rules_selected),
        "lifecycles": [runs.snapshots[run_id].lifecycle.value for run_id in result.run_ids],
        "receipts": [
            {"step": r.step_id, "kind": r.step_kind, "outcome": r.outcome,
             "transition": r.selected_transition, "inputs": dict(r.inputs),
             "result": dict(r.result)}
            for r in runs.receipts
        ],
    }


@pytest.mark.parametrize("scenario", SCENARIOS["scenarios"], ids=lambda s: s["name"])
async def test_scenario_has_identical_command_bytes_and_outcomes(scenario):
    before = await _execute(BEFORE, scenario)
    after = await _execute(AFTER, scenario)
    assert _json_bytes(after) == _json_bytes(before)
    assert after["rules"] == ([scenario["rule"]] if scenario["rule"] else [])
    assert after["lifecycles"] == ([scenario["lifecycle"]] if scenario["lifecycle"] else [])
    assert after["calls"] == (
        [{"command": scenario["command"], "inputs": scenario["inputs"]}]
        if scenario["command"] else []
    )
