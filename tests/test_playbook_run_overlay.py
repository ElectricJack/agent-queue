from __future__ import annotations

import json
from pathlib import Path

from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import load_definition_json
from src.playbooks.graph_projection import project_graph
from src.playbooks.run_overlay import project_overlay
from tests.playbook_v2_helpers import StubContracts, StubProfiles

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks" / "v2"


def _data():
    definition = load_definition_json((FIXTURES / "review-pipeline.artifact.json").read_text())
    ref = ArtifactRef(
        definition.id,
        definition.artifact_sha256(),
        2,
        definition.contract_fingerprint(),
        definition.source_hash,
        definition.compiler_build or "fixture",
        definition.compiled_at.isoformat(),
        definition.version,
    )
    receipts = json.loads((FIXTURES / "review-pipeline.receipts.json").read_text())["receipts"]
    run = {
        "run_id": "run-7",
        "playbook_id": definition.id,
        "artifact_sha256": ref.artifact_sha256,
        "rule_id": "review-on-task-completed",
        "lifecycle": "completed",
        "event_type": "task.completed",
        "event": {},
        "bindings": {},
        "budget": {"llm_calls": 1, "total_tokens": 3168},
    }
    return definition, ref, receipts, run


def _overlay(**kwargs):
    definition, ref, receipts, run = _data()
    return project_overlay(
        kwargs.pop("run", run),
        kwargs.pop("receipts", receipts),
        definition,
        ref,
        active_sha256=kwargs.pop("active_sha256", ref.artifact_sha256),
        **kwargs,
    )


def test_overlay_uses_the_pinned_artifact_not_the_activation():
    overlay = _overlay(active_sha256="sha256:" + "f" * 64)
    assert overlay["artifact_is_active"] is False
    assert overlay["artifact"]["artifact_sha256"] == _data()[1].artifact_sha256


def test_every_overlay_edge_id_exists_in_the_projected_graph():
    definition, ref, _receipts, _run = _data()
    graph = project_graph(
        definition, ref, None, contracts=StubContracts(), profiles=StubProfiles()
    )
    assert {edge["edge_id"] for edge in _overlay()["edges"]} <= {
        edge["id"] for edge in graph["edges"]
    }


def test_loop_iterations_are_listed_not_collapsed():
    node = next(item for item in _overlay()["nodes"] if item["step_id"] == "open-gate")
    assert node["visit_count"] == 5
    assert len(node["iterations"]) == 5
    assert sum(item["outcome"] == "rejected" for item in node["iterations"]) == 1


def test_multiple_attempts_on_one_step_are_both_present():
    receipts = [item for item in _overlay()["receipts"] if item["step_id"] == "ensure-review-task"]
    assert [item["attempt"] for item in receipts] == [1, 2]


def test_receipt_cap_sets_truncated():
    overlay = _overlay(receipt_limit=3)
    assert overlay["truncated"] is True
    assert overlay["receipt_total"] == 11
    assert len(overlay["receipts"]) == 3


def test_receipt_for_unknown_step_yields_diagnostic_not_crash():
    _definition, _ref, receipts, _run = _data()
    unknown = {**receipts[0], "receipt_id": "unknown", "step_id": "future-step"}
    overlay = _overlay(receipts=[*receipts, unknown])
    assert overlay["diagnostics"][0]["code"] == "unknown_receipt_step"
