"""Execute the request policy against the frozen pre-port shipped graph."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.models import CommandResult
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.profiles.capabilities import CapabilityPolicy
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingBus,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)


FIXTURES = Path(__file__).parent / "fixtures/playbooks"
SCENARIOS = FIXTURES / "scenarios/promotion-request"
BEFORE = SCENARIOS / "shipped-before.artifact.json"
AFTER = FIXTURES / "v2/promotion-request/artifact.json"
CASES = json.loads((SCENARIOS / "events.json").read_text())


def test_executable_graph_is_unchanged():
    # This is the existing reviewed golden, never rebuilt from the port.
    assert hashlib.sha256(BEFORE.read_bytes()).hexdigest() == (
        "e23b9ec529926ed9c6207b9f5843d77942bb9690bc9694d8c4b19938f1445a85"
    )
    before, after = [json.loads(path.read_text()) for path in (BEFORE, AFTER)]
    for graph in (before, after):
        for rule in graph["rules"]:
            rule.pop("source")
        for step in graph["steps"].values():
            step.pop("source")
    assert before["rules"] == after["rules"]
    assert before["steps"] == after["steps"]
    assert before["compiled_against"] == after["compiled_against"]


async def execute(path, case):
    artifact = load_definition_json(path.read_text())
    registry = ContractRegistry()
    calls = []
    policy = {
        "source_sha": "a" * 40,
        "backmerge_pending": False,
        "active_request_id": None,
    } | case.get("policy", {})
    responses = {
        "integration_promotion_policy_input": {"outcome": "policy_input", "policy": policy},
        "promote_request": {"outcome": "requested", "batch_id": "promotion-batch"},
        "integration_promotion_publish": {"outcome": "published"},
        "integration_backmerge_source": {"outcome": "backmerges_authored"},
        "message_send": {"outcome": "queued", "message_id": "notice", "state": "queued"},
    }
    responses.update(case.get("responses", {}))
    for name in {step.command for step in artifact.steps.values() if step.type == "command"}:
        registration = CONTRACTS.get(name)

        async def invoke(args, ctx, name=name, contract=registration.contract.execution):
            calls.append((name, args.model_dump(mode="json")))
            raw = responses[name]
            value = contract.result_model.model_validate({
                key: value for key, value in raw.items()
                if key in contract.result_model.model_fields
            })
            return CommandResult(outcome=raw["outcome"], value=value, summary="")

        registry.register(CommandRegistration(name, registration.contract, invoke))
    store, runs = InMemoryArtifactStore(), RecordingRunRepository()
    store.put(artifact)
    ref = artifact_ref_for(artifact)
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry, artifact_store=store, bus=RecordingBus(), clock=lambda: 1000.0,
        ),
        runs=runs,
        waits=None,
        activations=StubActivations([ref]),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=list(artifact.compiled_against.commands),
        ),
        service_name="fixture-promotion-request",
        project_id="p",
    )
    event = {
        "project_id": "p", "step_id": "release", "notes_reviewed": True,
        "event_id": case["id"], **case.get("event", {}),
    }
    for key in case.get("missing", []):
        event.pop(key)
    outcome = await engine.run_rule(ref, case["rule"], event, principal)
    trace = {
        "calls": copy.deepcopy(calls),
        "lifecycle": outcome.lifecycle.value,
        "receipts": [
            (r.step_id, r.receipt_kind, r.outcome, r.selected_transition, r.error_code)
            for r in runs.receipts
        ],
    }
    # Durable duplicate delivery must not send another message or run another command.
    replay = await engine.run_rule(ref, case["rule"], event, principal)
    assert replay.outcome == "deduplicated"
    assert calls == trace["calls"]
    if case.get("distinct_event"):
        await engine.run_rule(ref, case["rule"], {**event, "event_id": "next-event"}, principal)
        assert calls == trace["calls"] * 2
    return trace


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
async def test_scenario_equivalence(case):
    before, after = [await execute(path, case) for path in (BEFORE, AFTER)]
    # Command inputs (including notification wording/recipient/sender), decisions,
    # failures and terminal state must remain byte-for-byte equal.
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    assert [name for name, _ in after["calls"]] == case["calls"]
    assert after["lifecycle"] == case.get("lifecycle", "completed")
    assert all(not args.get("allow_breaking_migration") for _, args in after["calls"])


@pytest.mark.parametrize("step_id", [
    "explicit-request--read", "explicit-request--request", "explicit-request--notify",
    "hotfix-completed--read", "hotfix-completed--request",
    "visit-intent--publish", "backmerge-delivered--backmerge",
])
async def test_every_registered_outcome_preserves_trace(step_id):
    """Exercise every success/refusal edge, including all migration refusals."""
    before = load_definition_json(BEFORE.read_text())
    step = before.steps[step_id]
    for outcome in step.transitions:
        raw = {"outcome": outcome}
        if step.command == "integration_promotion_policy_input" and outcome == "policy_input":
            raw["policy"] = {
                "source_sha": "a" * 40, "backmerge_pending": False, "active_request_id": None,
            }
        case = {
            "id": f"{step_id}-{outcome}", "rule": step.rule,
            "event": {"from_task": "hotfix", "batch_id": "promotion-batch"},
            "responses": {step.command: raw},
        }
        if step.command == "message_send":
            case["policy"] = {"backmerge_pending": True}
        traces = [await execute(path, case) for path in (BEFORE, AFTER)]
        assert traces[0] == traces[1], (step_id, outcome)
        assert any(name == step.command for name, _ in traces[1]["calls"])
