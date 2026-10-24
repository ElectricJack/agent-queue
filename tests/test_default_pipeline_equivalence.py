"""Compare the building-block declaration with the captured shipped pipeline.

Only the command handler's responses are substituted. Real dispatch, guards,
expression resolution, typed adapters, transitions and receipts execute for
both artifacts. Database effects and ingest receipt reuse are covered by the
unchanged test_default_pipeline_intake.py suite.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
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
from src.playbooks.profiles import shipped_profile_lookup
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


def test_captured_baseline_and_existing_executable_rules_are_unchanged():
    before = load_definition_json(BEFORE.read_text())
    after = load_definition_json(AFTER.read_text())
    assert "sha256:" + hashlib.sha256(BEFORE.read_bytes()).hexdigest() == (
        SCENARIOS["baseline_artifact_sha256"]
    )
    assert canonical_bytes(before) == BEFORE.read_bytes()
    assert before.source_hash == SCENARIOS["baseline_source_sha256"]

    original_rules = {rule.id for rule in before.rules}
    assert {rule.id for rule in after.rules} == original_rules | {
        "fidelity-review-on-held", "release-on-receipt",
    }
    # Held-batch rules and their exact refusal handling are pinned independently
    # by test_default_playbook_v2_artifacts and the real intake tests.
    policy = shipped_profile_lookup().policy("spec-ingest")
    assert {"wait_checkpoint", "wait_get", "wait_register"} <= policy.aq_commands
    assert after.compiled_against.profiles["spec-ingest"] == policy.fingerprint()

    def executable(artifact):
        body = json.loads(canonical_bytes(artifact))
        # Compilation provenance changes with the source; executable fields do not.
        for field in ("source_hash", "compiled_at", "trusted_execution"):
            body.pop(field, None)
        body["rules"] = [rule for rule in body["rules"] if rule["id"] in original_rules]
        body["steps"] = {
            name: step for name, step in body["steps"].items() if step["rule"] in original_rules
        }
        body["compiled_against"]["commands"] = {
            name: body["compiled_against"]["commands"][name]
            for name in before.compiled_against.commands
        }
        # Role instructions now describe held batches; their fingerprints are
        # checked against the shipped profiles by the artifact suite.
        body["compiled_against"].pop("profiles")
        for item in [*body["rules"], *body["steps"].values()]:
            item.pop("source")
        return _json_bytes(body)

    assert executable(after) == executable(before)
    assert set(after.trusted_execution.required_capabilities) == {
        "ensure_spec_ingest", "gate_create", "task_batch_commit",
        "ensure_fidelity_review", "task_batch_release",
    }
    assert not after.required_features
    assert not after.trusted_execution.scripts
    assert after.trusted_execution.state_schema_version is None


@pytest.mark.parametrize("composed", [False, True], ids=["original", "held-batch-composition"])
def test_wait_grant_refresh_preserves_the_complete_previous_artifact(composed):
    evidence = json.loads(Path(
        "tests/fixtures/contracts/default-pipeline-wait-grants-changes.json"
    ).read_text())
    if composed:
        evidence = json.loads(Path(
            "tests/fixtures/contracts/default-pipeline-wait-grants-composition.json"
        ).read_text())
    current_path = AFTER if composed else FIXTURES / "wait-grants.artifact.json"
    change = evidence["playbook_fixtures"][AFTER.as_posix()]
    previous_bytes = Path(change["preserved_previous_path"]).read_bytes()
    assert hashlib.sha256(previous_bytes).hexdigest() == change["previous_sha256"]
    assert hashlib.sha256(current_path.read_bytes()).hexdigest() == change["sha256"]
    previous = json.loads(previous_bytes)
    current = json.loads(current_path.read_bytes())
    grants = evidence["profile_grants"]["spec-ingest"]
    assert grants["added_aq_commands"] == ["wait_checkpoint", "wait_get", "wait_register"]
    assert previous["compiled_against"]["profiles"]["spec-ingest"] == grants["previous_fingerprint"]
    assert current["compiled_against"]["profiles"]["spec-ingest"] == grants["fingerprint"]
    policy = shipped_profile_lookup().policy("spec-ingest")
    if not composed:
        # The original wait-grant branch predates the held-batch grant removal.
        policy = replace(policy, aq_commands=policy.aq_commands | {"task_batch_commit"})
    assert policy.fingerprint() == grants["fingerprint"]
    assert replace(
        policy, aq_commands=policy.aq_commands - set(grants["added_aq_commands"]),
    ).fingerprint() == grants["previous_fingerprint"]
    previous["compiled_against"]["profiles"] = current["compiled_against"]["profiles"]
    previous.pop("compiled_at")
    current.pop("compiled_at")
    assert current == previous


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
