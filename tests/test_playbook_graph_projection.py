from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import load_definition_json
from src.playbooks.graph_projection import GraphProjectionError, project_graph
from tests.playbook_v2_helpers import StubContracts, StubProfiles

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks" / "v2"


def _definition():
    return load_definition_json((FIXTURES / "review-pipeline.artifact.json").read_text())


def _ref(definition):
    return ArtifactRef(
        playbook_id=definition.id,
        artifact_sha256=definition.artifact_sha256(),
        schema_generation=definition.schema_version,
        contract_fingerprint=definition.contract_fingerprint(),
        source_digest=definition.source_hash,
        compiler_build=definition.compiler_build or "fixture",
        compiled_at=definition.compiled_at.isoformat(),
        version=definition.version,
    )


def _project(definition=None, **kwargs):
    definition = definition or _definition()
    contracts = kwargs.pop("contracts", StubContracts())
    return project_graph(
        definition,
        _ref(definition),
        None,
        contracts=contracts,
        profiles=StubProfiles(),
        **kwargs,
    )


def test_one_edge_per_transition_record():
    definition = _definition()
    expected = {
        f"{step.rule}::{step_id}::{outcome}"
        for step_id, step in definition.steps.items()
        for outcome in getattr(step, "transitions", {})
    }
    expected |= {
        f"{step.rule}::{step_id}::case:{index}"
        for step_id, step in definition.steps.items()
        for index, _case in enumerate(getattr(step, "cases", ()))
    }
    expected |= {
        f"{step.rule}::{step_id}::default"
        for step_id, step in definition.steps.items()
        if hasattr(step, "default")
    }
    expected |= {
        f"{step.rule}::{step_id}::body"
        for step_id, step in definition.steps.items()
        if hasattr(step, "body_entry")
    }
    actual = [edge["id"] for edge in _project(definition)["edges"]]
    assert set(actual) == expected
    assert len(actual) == len(set(actual)) == 34


def test_timeout_edge_survives_when_it_shares_a_target():
    graph = _project()
    edges = [edge for edge in graph["edges"] if edge["source"] == "classify-risk"]
    assert {edge["outcome"] for edge in edges if edge["target"] == "review-unavailable"} >= {
        "timed_out",
        "provider_error",
    }


def test_no_edge_crosses_a_rule_cluster():
    definition = _definition()
    step = definition.steps["ensure-review-task"].model_copy(
        update={"transitions": {"created": "sweep-done"}}
    )
    broken = definition.model_copy(update={"steps": {**definition.steps, "ensure-review-task": step}})
    with pytest.raises(GraphProjectionError, match="crosses rule cluster"):
        _project(broken)


def test_shared_terminal_titles_do_not_merge_nodes():
    graph = _project()
    terminals = [node for node in graph["nodes"] if node["step_kind"] == "terminal"]
    assert len(terminals) == 5
    assert len({node["id"] for node in terminals}) == 5


def test_event_filter_preserves_every_reachable_branch():
    complete = _project()
    filtered = _project(event_type="task.completed")
    rule_ids = {rule["rule_id"] for rule in filtered["rules"]}
    assert rule_ids == {"review-on-task-completed"}
    assert {node["id"] for node in filtered["nodes"]} == {
        node["id"] for node in complete["nodes"] if node["rule_id"] in rule_ids
    }
    assert {edge["id"] for edge in filtered["edges"]} == {
        edge["id"] for edge in complete["edges"] if edge["rule_id"] in rule_ids
    }
    assert len(filtered["event_groups"]) == 2


def test_explanation_is_copied_not_rederived(monkeypatch):
    from src.playbooks.validation import RegistryContractLookup

    sentinel = {
        "title": "sentinel",
        "effect_summary": "copied",
        "effects": [],
        "inputs": [],
        "result": None,
        "outcomes": [],
        "contract_fingerprint": None,
        "renderer": "canonical",
    }
    monkeypatch.setattr(
        "src.playbooks.graph_projection.render_node_explanation", lambda *a, **k: sentinel
    )
    graph = _project(contracts=RegistryContractLookup())
    command = next(node for node in graph["nodes"] if node["id"] == "ensure-review-task")
    assert command["explanation"] == sentinel


def test_missing_contract_yields_canonical_renderer_and_error_diagnostic():
    graph = _project()
    node = next(item for item in graph["nodes"] if item["id"] == "ensure-review-task")
    assert node["explanation"]["renderer"] == "canonical"
    assert [(item["severity"], item["code"]) for item in node["diagnostics"]] == [
        ("error", "unknown_command")
    ]
    assert all(item["value"]["canonical"] is None for item in node["explanation"]["inputs"])
    assert all(
        value == {"type": "unresolved"}
        for value in node["advanced"]["typed_step"]["inputs"].values()
    )


def test_projection_is_deterministic():
    first = json.dumps(_project(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(_project(), sort_keys=True, separators=(",", ":"))
    assert first == second
