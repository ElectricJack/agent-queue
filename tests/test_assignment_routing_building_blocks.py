"""Installed and shipped routers retain their own policy and command traces.

Frozen inputs predate this port. The installed policy is newer than the shipped
one; comparing each with its own baseline prevents accidentally rolling it back.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import TaskRouteApplyValue, TaskRoutePlanValue
from src.commands.contracts.models import CommandResult
from src.playbooks.definition import (
    granted_aq_commands,
    load_definition_json,
    source_digest,
)
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import EXECUTORS, ExecutionMode
from src.playbooks.executors.base import EngineServices
from tests._private_values_helper import ReadyPrivateValues
from tests.fixtures.contracts.engine_contracts import registry_with
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)
from tests.test_default_assignment_routing_playbook import (
    _ScriptedLlm,
    _create,
    _dispatch,
    _principal,
    handler as handler,
)

BASE = Path("tests/fixtures/playbooks/routing-building-blocks")
PORTED = {
    "installed": Path("docs/playbooks/building-blocks/default-assignment-routing"),
    "shipped": Path("tests/fixtures/playbooks/v2/default-assignment-routing"),
}
SCENARIOS = json.loads((BASE / "scenarios.json").read_text())
COMMANDS = {"task_route_plan", "task_route_apply"}


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("variant", PORTED)
def test_frozen_policy_prompt_and_entire_executable_graph_are_unchanged(variant):
    hashes = json.loads((BASE / "baseline-sha256.json").read_text())[variant]
    for name, digest in hashes.items():
        assert "sha256:" + hashlib.sha256((BASE / variant / name).read_bytes()).hexdigest() == digest
    before = json.loads((BASE / variant / "artifact.json").read_text())
    after = json.loads((PORTED[variant] / "artifact.json").read_text())
    definition = load_definition_json(json.dumps(after))
    baseline = load_definition_json(json.dumps(before))
    assert granted_aq_commands(definition) == granted_aq_commands(baseline) == COMMANDS
    assert definition.source_hash == source_digest((PORTED[variant] / "source.md").read_text())
    assert definition.trusted_execution.required_capabilities == tuple(sorted(COMMANDS))
    assert definition.trusted_execution.state_schema_version is None
    assert definition.required_features == baseline.required_features
    assert json.loads((PORTED[variant] / "diagnostics.json").read_text()) == []
    assert before.get("trusted_execution") is None
    after.pop("trusted_execution")
    # Only provenance moves: source lines/hash, compile time, and the installed
    # compiler profile pin (the draft is compiled against this checkout).
    for payload in (before, after):
        payload.pop("source_hash")
        payload.pop("compiled_at")
        if variant == "installed":
            payload["compiled_against"].pop("profiles")
        for item in [*payload["rules"], *payload["steps"].values()]:
            item.pop("source")
    assert _json_bytes(after) == _json_bytes(before)


async def _trace(path, scenario, monkeypatch):
    artifact = load_definition_json(path.read_text())
    registry, adapter = registry_with(*(CONTRACTS.require(name).contract for name in COMMANDS))
    for index, outcome in enumerate(scenario["plans"]):
        value = TaskRoutePlanValue(**SCENARIOS["plan_value"], reason=f"plan-{index}")
        adapter.script("task_route_plan", CommandResult(outcome=outcome, value=value, summary="plan"))
    if scenario.get("apply"):
        adapter.script("task_route_apply", CommandResult(
            outcome=scenario["apply"], value=TaskRouteApplyValue(task_id="task-1"), summary="apply",
        ))
    # The live LLM executor validates JSON and emits invalid_output. Its result
    # helper alone does not validate, so script that boundary outcome explicitly.
    llm_outcome = {"invalid": "invalid_output", "runtime_error": "runtime_error"}.get(
        scenario.get("llm"), "completed",
    )
    answer = SCENARIOS["classification"] if llm_outcome == "completed" else None
    llm = _ScriptedLlm(answer, llm_outcome)
    monkeypatch.setitem(EXECUTORS[ExecutionMode.LIVE], "llm", llm)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry, clock=lambda: 123.0, private_values=ReadyPrivateValues(),
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
        ),
        runs=runs, waits=runs, activations=StubActivations([artifact_ref_for(artifact)]),
    )
    event = copy.deepcopy(SCENARIOS["event"])
    event.update(scenario.get("event", {}))
    event["task"].update(scenario.get("task", {}))
    result = await engine.dispatch_event(event, _principal())
    snapshots = list(runs.snapshots.values())
    terminal = snapshots[0].lifecycle.value if snapshots else None
    steps = [r.step_id.removeprefix("route-task--")
             for r in runs.receipts if r.receipt_kind == "step"]
    assert steps == scenario["steps"]
    assert terminal == scenario["terminal"]
    assert not any(adapter.scripts.values()), "a scripted command was never reached"
    # Full typed command arguments and bound values, not just the chosen edge.
    # Omit only execution identity/provenance (run ids, artifact hash, caller
    # record), which necessarily changes when a new artifact is compiled.
    trace = {
        "selected": result.rules_selected, "terminal": terminal, "steps": steps,
        "calls": [(name, args.model_dump(mode="json")) for name, args, _ in adapter.calls],
        "authority": [(p.kind.value, p.project_id, sorted(p.policy.aq_commands))
                      for _, _, p in adapter.calls],
        "prompts": llm.prompts, "classifier_inputs": llm.inputs,
        "bindings": [dict(run.bindings) for run in snapshots],
    }
    return _json_bytes(trace)


@pytest.mark.parametrize("variant", PORTED)
@pytest.mark.parametrize("scenario", SCENARIOS["cases"], ids=lambda case: case["name"])
async def test_representative_events_have_byte_equal_command_traces(variant, scenario, monkeypatch):
    before = await _trace(BASE / variant / "artifact.json", scenario, monkeypatch)
    after = await _trace(PORTED[variant] / "artifact.json", scenario, monkeypatch)
    assert after == before


@pytest.mark.parametrize("revision", ["baseline", "ported"])
async def test_installed_policy_keeps_claude_tie_order_with_real_handler(
    handler, monkeypatch, revision,
):
    """The installed fallback differs from shipped Codex preference on purpose."""
    directory = BASE / "installed" if revision == "baseline" else PORTED["installed"]
    monkeypatch.setattr(
        "tests.test_default_assignment_routing_playbook.FIXTURE", directory / "artifact.json",
    )
    task = await _create(handler, "installed-fallback")
    _, runs = await _dispatch(handler, task, _ScriptedLlm(None, "runtime_error"), monkeypatch)
    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    routed = await handler.db.get_task(task.id)
    assert (routed.route_source, routed.profile_id, routed.intelligence_class) == (
        "router", "standard-high-claude", "standard-high",
    )
    assert routed.route["classification"]["failed"] is True
    assert routed.route["rule"] == "kinds.feature"
