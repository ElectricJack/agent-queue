"""The intent an operator reads and the contract that runs are one thing.

Package 1's last required outcome: "a contract test proving the displayed
explanation and invoked contract share the same registration and fingerprint".
That is a cross-module invariant — registry, renderer, and graph view — so it
lives here rather than in any single module's suite.  The golden payloads in
``tests/fixtures/contracts/`` are the same bytes the dashboard fixtures import,
so the two suites cannot drift into asserting different rendered copy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS
from src.playbooks.graph_view import build_edges, build_graph_view
from src.playbooks.models import CompiledPlaybook
from src.playbooks.pipeline_compiler import compile_pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"
# The frozen pre-Package-6 V1 graph: the shipped Markdown is a prose
# authoring source now and carries no machine graph for V1 to compile.
# See tests/fixtures/playbooks/v1/README.md.
DEFAULT_PIPELINE = Path(__file__).parent / "fixtures" / "playbooks" / "v1" / "default-pipeline.md"


def _compile(path: Path) -> CompiledPlaybook:
    result = compile_pipeline(path.read_text())
    assert result.success, result.errors
    assert result.playbook is not None
    return result.playbook


@pytest.fixture(scope="module")
def fixture_playbook() -> CompiledPlaybook:
    return _compile(FIXTURES / "pipeline-intent.md")


@pytest.fixture(scope="module")
def default_pipeline() -> CompiledPlaybook:
    return _compile(DEFAULT_PIPELINE)


def _explained(playbook: CompiledPlaybook) -> dict[str, dict]:
    view = build_graph_view(playbook)
    return {n["id"]: n for n in view["graph"]["nodes"] if n.get("explanation")}


def _action_nodes(playbook: CompiledPlaybook) -> set[str]:
    return {
        nid
        for nid, node in playbook.nodes.items()
        if node.action and isinstance(node.action.get("command"), str)
    }


@pytest.mark.parametrize("source", ["fixture", "default"])
def test_every_contracted_action_node_is_explained(
    source, fixture_playbook, default_pipeline
) -> None:
    playbook = fixture_playbook if source == "fixture" else default_pipeline
    assert _action_nodes(playbook) == set(_explained(playbook))


@pytest.mark.parametrize("source", ["fixture", "default"])
def test_displayed_fingerprint_is_the_registry_fingerprint(
    source, fixture_playbook, default_pipeline
) -> None:
    playbook = fixture_playbook if source == "fixture" else default_pipeline
    for nid, node in _explained(playbook).items():
        explanation = node["explanation"]
        command = playbook.nodes[nid].action["command"]
        assert explanation["command"] == command
        assert explanation["contract_fingerprint"] == CONTRACTS.fingerprint(command)
        assert explanation["capability"] == CONTRACTS.required_capability(command)


@pytest.mark.parametrize("source", ["fixture", "default"])
def test_displayed_outcomes_are_exactly_the_executable_action_edges(
    source, fixture_playbook, default_pipeline
) -> None:
    """Every executable action transition is displayed; every displayed one runs.

    The ``edge_type`` filter is load-bearing: ``build_edges`` also emits a
    ``timeout`` edge, which is not an action outcome.
    """
    playbook = fixture_playbook if source == "fixture" else default_pipeline
    edges = build_edges(playbook)
    for nid, node in _explained(playbook).items():
        displayed = {
            outcome["target_node_id"]
            for outcome in node["explanation"]["outcomes"]
            if outcome["target_node_id"]
        }
        executable = {
            edge["target"]
            for edge in edges
            if edge["source"] == nid and edge["edge_type"] in {"success", "failure"}
        }
        assert displayed == executable


@pytest.mark.parametrize("source", ["fixture", "default"])
def test_every_executable_argument_is_displayed_exactly_once(
    source, fixture_playbook, default_pipeline
) -> None:
    playbook = fixture_playbook if source == "fixture" else default_pipeline
    for nid, node in _explained(playbook).items():
        explanation = node["explanation"]
        shown = [i["field"] for i in explanation["inputs"]] + explanation["unrendered_fields"]
        assert sorted(shown) == sorted(playbook.nodes[nid].action["args"])
        assert len(shown) == len(set(shown))


@pytest.mark.parametrize("source", ["fixture", "default"])
def test_no_rendered_value_is_empty_or_the_word_none(
    source, fixture_playbook, default_pipeline
) -> None:
    playbook = fixture_playbook if source == "fixture" else default_pipeline
    for node in _explained(playbook).values():
        explanation = node["explanation"]
        assert explanation["title"] and explanation["effects"]
        for item in explanation["inputs"]:
            assert item["label"] and item["value"]["text"] not in ("", "None")
        for effect in explanation["effects"]:
            assert effect["text"] and effect["text"] != "None"
        if loop := explanation["loop"]:
            assert loop["source_text"] != "None" and loop["source_raw"] != "None"


@pytest.mark.parametrize(
    ("node_id", "golden"),
    [
        ("per-task-review-create-review", "explanation-create-review.json"),
        ("per-task-review-gate-downstream", "explanation-gate-downstream.json"),
    ],
)
def test_fixture_node_matches_its_golden(node_id, golden, fixture_playbook) -> None:
    """The goldens are what the dashboard fixtures import — one source of truth.

    Regenerate them deliberately: a diff here is a change to what every
    operator reads about these commands.
    """
    rendered = dict(_explained(fixture_playbook)[node_id]["explanation"])
    # Asserted against the live registry rather than pinned in the file, so a
    # contract change breaks one focused test instead of every golden.
    assert rendered.pop("contract_fingerprint") == CONTRACTS.fingerprint(rendered["command"])
    assert rendered == json.loads((FIXTURES / golden).read_text())


def test_the_default_pipeline_reads_its_arguments_against_the_triggering_event(
    default_pipeline,
) -> None:
    """Event copy and event redaction need the node's own trigger event type.

    Without it every ``{{event.*}}`` argument renders as ``unresolved`` and the
    event-field sensitivity registry is never consulted — the shipped pipeline
    would show raw expressions and could display a value marked sensitive.
    """
    from src.playbooks.graph_view import node_event_types

    events = node_event_types(default_pipeline)
    assert set(events) == set(default_pipeline.nodes)
    assert events["per-task-review-create-review"] == "task.completed"
    assert events["commit-on-gate-resolve-commit_proposal"] == "gate.resolved"

    values = {
        (nid, item["field"]): item["value"]
        for nid, node in _explained(default_pipeline).items()
        for item in node["explanation"]["inputs"]
    }
    assert values[("per-task-review-create-review", "project_id")] == {
        "kind": "event_ref",
        "text": "this event's project",
        "raw": "{{event.project_id}}",
        "redacted": False,
    }
    assert values[("commit-on-gate-resolve-commit_proposal", "proposal_id")]["text"] == (
        "this event's await identifier"
    )
    assert not [v for v in values.values() if v["kind"] == "unresolved"]
