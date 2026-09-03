from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import load_definition_json
from src.playbooks.graph_projection import GraphProjectionError, project_graph
from src.profiles.intelligence import ProfileIntelligence
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
    profiles = kwargs.pop("profiles", None)
    return project_graph(
        definition,
        _ref(definition),
        None,
        contracts=contracts,
        profiles=StubProfiles() if profiles is None else profiles,
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


def test_the_card_title_is_the_authored_step_title_not_the_command_title():
    """One step, one name (§6.2): the card and the canvas node agree.

    ``RegistryContractLookup`` is what the daemon projects with, so every
    command in the artifact takes the contract path.  A contract's
    ``presentation.title`` names the *command* — "Ensure a task exists" — while
    the authored step title names this *use* of it, and it is the more specific
    of the two.  The card takes the authored title.
    """
    from src.commands.contracts import CONTRACTS
    from src.playbooks.validation import RegistryContractLookup

    graph = _project(contracts=RegistryContractLookup())
    node = _node(graph, "ensure-review-task")
    assert node["explanation"]["renderer"] == "contract"
    assert node["title"] == "Ensure a review task"
    assert node["explanation"]["title"] == "Ensure a review task"
    # The contract does carry a title of its own; it just does not win here.
    assert CONTRACTS.require("ensure_task").contract.presentation.title != node["title"]
    # And it is not one node: the invariant holds down both renderer paths.
    assert all(item["explanation"]["title"] == item["title"] for item in graph["nodes"])
    assert all(item["explanation"]["title"] == item["title"] for item in _project()["nodes"])


def test_a_step_with_no_title_of_its_own_falls_back_to_the_contract_title():
    """``presentation.title`` is the fallback, not the preference.

    A step that declares no title has nothing more specific to show, so the
    contract's name for the command is better than an empty card heading.
    """
    from src.playbooks.validation import RegistryContractLookup

    def untitled(step_id):
        definition = _definition()
        step = definition.steps[step_id].model_copy(update={"title": "  "})
        return definition.model_copy(update={"steps": {**definition.steps, step_id: step}})

    graph = _project(untitled("ensure-review-task"), contracts=RegistryContractLookup())
    assert _node(graph, "ensure-review-task")["explanation"]["title"] == "Ensure a task exists"
    # With no contract to fall back to either, the command name is the last
    # honest heading — ``list_tasks`` is the artifact's unregistered command.
    graph = _project(untitled("list-downstream"), contracts=StubContracts())
    node = _node(graph, "list-downstream")
    assert node["explanation"]["renderer"] == "canonical"
    assert node["explanation"]["title"] == "list_tasks"


def test_projection_is_deterministic():
    first = json.dumps(_project(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(_project(), sort_keys=True, separators=(",", ":"))
    assert first == second


def test_ai_nodes_carry_the_resolved_intelligence_class_provider_and_model():
    """Package 5's AI cards state provider/model policy, not just capabilities."""
    graph = _project()
    ai_nodes = {node["id"]: node["ai"] for node in graph["nodes"] if node["ai"]}
    assert set(ai_nodes) == {"classify-risk", "escalate"}
    for node_id, ai in ai_nodes.items():
        assert ai["profile_id"] == "reviewer", node_id
        assert (ai["intelligence_class"], ai["provider"], ai["model"]) == (
            "deep-high",
            "anthropic",
            "claude-opus-5",
        ), node_id


def test_ai_routing_comes_from_the_profile_lookup_not_the_step():
    graph = _project(profiles=StubProfiles(routing={"reviewer": ProfileIntelligence(
        "fast-low", "google", "gemini-3-flash"
    )}))
    ai = next(node["ai"] for node in graph["nodes"] if node["id"] == "classify-risk")
    assert (ai["intelligence_class"], ai["provider"], ai["model"]) == (
        "fast-low",
        "google",
        "gemini-3-flash",
    )


def test_a_profile_the_lookup_does_not_know_reports_no_routing():
    graph = _project(profiles=StubProfiles(routing={}))
    ai = next(node["ai"] for node in graph["nodes"] if node["id"] == "escalate")
    assert (ai["intelligence_class"], ai["provider"], ai["model"]) == (None, None, None)
    # The capability half of the card is unaffected.
    assert ai["capabilities"]["aq_commands"] == ["demo_command"]


def test_a_lookup_without_routing_degrades_instead_of_raising():
    class PolicyOnly:
        def policy(self, profile_id: str):
            return StubProfiles().policy(profile_id)

    ai = next(
        node["ai"]
        for node in _project(profiles=PolicyOnly())["nodes"]
        if node["id"] == "classify-risk"
    )
    assert (ai["intelligence_class"], ai["provider"], ai["model"]) == (None, None, None)
    assert ai["capability_fingerprint"]


# --------------------------------------------------------------------------
# agile-cascade-53 — every step family explains its reads, its writes and its
# intent.  Before this, six of the seven families shared one generic clause
# and reported ``inputs=[]`` / ``result=None``.
# --------------------------------------------------------------------------


def _node(graph, step_id):
    return next(node for node in graph["nodes"] if node["id"] == step_id)


def _inputs(graph, step_id):
    return {
        row["label"]: (row["value"]["display"], row["value"]["kind"], row["source"])
        for row in _node(graph, step_id)["explanation"]["inputs"]
    }


def test_llm_step_explains_its_prompt_its_budget_and_its_binding():
    graph = _project()
    node = _node(graph, "classify-risk")
    explanation = node["explanation"]
    assert explanation["effect_summary"] == (
        "Ask the reviewer profile for a structured answer and branch on its risk"
    )
    assert _inputs(graph, "classify-risk") == {
        "Prompt": ("Assess the review risk of task this event's title", "template", "template")
    }
    assert explanation["result"]["label"] == "risk"
    assert explanation["result"]["value"]["kind"] == "binding_ref"
    assert [(item["kind"], item["subject"]) for item in explanation["effects"]] == [
        ("invokes_ai", "reviewer"),
        ("binds", "risk"),
    ]
    assert "8000 total tokens" in explanation["effects"][0]["detail"]
    assert {badge["kind"] for badge in node["badges"]} == {"profile", "budget"}


def test_agent_task_step_explains_its_objective_and_its_delegation():
    graph = _project()
    explanation = _node(graph, "escalate")["explanation"]
    assert explanation["effect_summary"] == (
        "Delegate a task to the reviewer profile and wait for it"
    )
    assert _inputs(graph, "escalate") == {
        "Objective": ("Re-review the change and record the riskiest line", "literal", "literal")
    }
    assert explanation["result"]["label"] == "escalation"
    assert [item["kind"] for item in explanation["effects"]] == ["delegates", "binds"]


def test_agent_task_step_badges_whether_a_cancel_takes_the_child_with_it():
    """§6.2 puts ``cancel_child`` on an agent-task card.  A rule that leaves a
    child agent running after it is cancelled is a fleet an operator has to go
    and clean up, so the card says which it is either way."""
    node = _node(_project(), "escalate")
    assert {"kind": "wait", "label": "On cancel", "value": "leaves the child running"} in node[
        "badges"
    ]


def test_command_cards_badge_the_idempotency_their_contract_declares():
    """§6.2's command row: an operator re-dispatching an event needs to know
    from the card whether running the step twice runs it twice.

    Projected against the **real** contract registry rather than the suite's
    stub, because the mode lives on the registered contract and a stub that
    knows no commands cannot exercise it — which is how the missing chip
    survived the exit gate (``solid-harbor.49`` pass 2).
    """
    from src.playbooks.validation import RegistryContractLookup

    graph = _project(contracts=RegistryContractLookup())
    badges = {
        step_id: [badge for badge in _node(graph, step_id)["badges"] if badge["kind"] == "idempotency"]
        for step_id in ("ensure-review-task", "list-downstream", "open-gate")
    }
    assert badges == {
        # ``ensure_task`` dedups on an argument, ``list_tasks`` is a read and is
        # naturally idempotent, ``gate_create`` dedups on its await id.
        "ensure-review-task": [
            {"kind": "idempotency", "label": "Idempotent", "value": "keyed on dedup_key"}
        ],
        "list-downstream": [{"kind": "idempotency", "label": "Idempotent", "value": "natural"}],
        "open-gate": [
            {"kind": "idempotency", "label": "Idempotent", "value": "keyed on await_id"}
        ],
    }


def test_an_unregistered_command_claims_no_idempotency():
    """The stub registry knows none of the artifact's commands.  A card that
    said "Idempotent none" there would be asserting something the projection
    cannot know; the ``unknown_command`` diagnostic is the honest answer."""
    graph = _project()
    for step_id in ("ensure-review-task", "list-downstream", "open-gate"):
        node = _node(graph, step_id)
        assert [badge["kind"] for badge in node["badges"]] == ["diagnostic"], step_id


def test_wait_step_explains_what_it_awaits_and_how_it_correlates():
    graph = _project()
    node = _node(graph, "await-approval")
    explanation = node["explanation"]
    assert explanation["effect_summary"] == "Pause until a human resolves the gate"
    assert _inputs(graph, "await-approval") == {
        "Awaited": ("Approve the review", "literal", "literal"),
        "Correlation key": ("review.task_id", "binding_ref", "binding"),
    }
    assert explanation["result"]["label"] == "approval"
    detail = explanation["effects"][0]["detail"]
    assert "resolutions: approve, revise" in detail
    assert "times out after 86400s" in detail
    assert {"kind": "wait", "label": "Waits for", "value": "human"} in node["badges"]


def test_foreach_step_explains_its_collection_its_item_and_its_failure_policy():
    graph = _project()
    node = _node(graph, "for-each-task")
    explanation = node["explanation"]
    assert explanation["effect_summary"] == (
        "Run Open a spec-ingest gate once per item in downstream.tasks"
    )
    assert _inputs(graph, "for-each-task") == {
        "Collection": ("downstream.tasks", "binding_ref", "binding")
    }
    assert [(item["kind"], item["subject"]) for item in explanation["effects"]] == [
        ("branches", "task"),
        ("binds", "task"),
    ]
    assert "collects every failing item" in explanation["effects"][0]["detail"]
    assert {"kind": "loop", "label": "Failure policy", "value": "collect"} in node["badges"]


def test_decision_step_renders_each_case_condition_as_readable_text():
    graph = _project()
    explanation = _node(graph, "check-gate")["explanation"]
    assert _inputs(graph, "check-gate") == {
        "already open": ("gate.created == false", "expression", "binding")
    }
    assert explanation["inputs"][0]["value"]["canonical"]["type"] == "comparison"
    assert [
        (item["kind"], item["subject"], item["conditional_on"])
        for item in explanation["effects"]
    ] == [
        ("branches", "for-each-task", "gate.created == false"),
        ("branches", "for-each-task", None),
    ]


def test_decision_edge_condition_is_readable_not_canonical_json():
    edge = next(
        item
        for item in _project()["edges"]
        if item["id"] == "sweep-on-spec-approved::check-gate::case:0"
    )
    assert edge["condition"] == "gate.created == false"


def test_terminal_step_explains_its_outcome_and_its_returned_result():
    graph = _project()
    explanation = _node(graph, "done")["explanation"]
    assert explanation["effect_summary"] == "End the rule as completed"
    assert explanation["effects"] == [
        {
            "kind": "noop",
            "subject": "rule",
            "detail": "End the rule as completed",
            "arguments": [],
            "conditional_on": None,
        }
    ]
    assert explanation["result"] is None

    raw = json.loads((FIXTURES / "review-pipeline.artifact.json").read_text())
    raw["steps"]["done"]["result"] = {
        "type": "binding_ref",
        "binding": "review",
        "path": "task_id",
    }
    node = _node(_project(load_definition_json(json.dumps(raw))), "done")
    assert node["explanation"]["result"]["label"] == "Result"
    assert node["explanation"]["result"]["value"]["display"] == "review.task_id"
    assert node["explanation"]["result"]["source"] == "binding"
    assert "returning review.task_id" in node["explanation"]["effects"][0]["detail"]


def test_every_declared_value_a_step_reads_reaches_the_card():
    """Data reads: no typed expression in the artifact is dropped."""
    definition = _definition()
    graph = _project(definition)
    reads = {
        "classify-risk": {"Prompt"},
        "escalate": {"Objective"},
        "await-approval": {"Awaited", "Correlation key"},
        "for-each-task": {"Collection"},
        "check-gate": {"already open"},
    }
    for step_id, labels in reads.items():
        assert set(_inputs(graph, step_id)) == labels, step_id
        assert all(
            row["value"]["display"] for row in _node(graph, step_id)["explanation"]["inputs"]
        )
    # And the values that are dropped are exactly the steps that read nothing.
    empty = {
        node["id"] for node in graph["nodes"] if not node["explanation"]["inputs"]
    }
    assert empty == {
        step_id
        for step_id, step in definition.steps.items()
        if step.type == "terminal"
    }


def test_every_binding_a_step_writes_reaches_the_card_with_its_schema():
    """Data writes: ``save_result_as`` is a result row for every family."""
    from src.playbooks.definition import result_schema_for

    definition = _definition()
    graph = _project(definition)
    written = {
        step_id: step.save_result_as
        for step_id, step in definition.steps.items()
        if getattr(step, "save_result_as", None)
    }
    assert set(written) == {
        "ensure-review-task",
        "classify-risk",
        "escalate",
        "await-approval",
        "list-downstream",
        "open-gate",
    }
    for step_id, binding in written.items():
        node = _node(graph, step_id)
        result = node["explanation"]["result"]
        assert result is not None and result["label"] == str(binding), step_id
        assert result["value"]["kind"] == "binding_ref"
        assert result["value"]["display"] == str(binding)
        assert result["source"] == "derived"
        if node["step_kind"] != "command":
            assert node["advanced"]["result_schema"] == result_schema_for(
                definition.steps[step_id]
            )
    for step_id in definition.steps:
        if step_id not in written:
            assert _node(graph, step_id)["explanation"]["result"] is None, step_id


def test_every_node_carries_what_the_compact_card_and_the_inspector_read():
    """The card and the inspector consume one explanation object (§4.2)."""
    graph = _project()
    for node in graph["nodes"]:
        explanation = node["explanation"]
        # Compact card: title, one-line intent, chips, and a port per edge.
        assert explanation["title"] == node["title"]
        assert explanation["effect_summary"].strip()
        assert node["out_degree"] == sum(
            edge["source"] == node["id"] for edge in graph["edges"]
        )
        assert {badge["kind"] for badge in node["badges"]} <= {
            "profile",
            "budget",
            "capability",
            "timeout",
            "retry",
            "idempotency",
            "loop",
            "wait",
            "redaction",
            "diagnostic",
        }
        # Inspector: the same rows, plus the canonical payload behind them.
        assert node["advanced"]["resolved_inputs"] == explanation["inputs"]
        assert node["advanced"]["typed_step"]["type"] == node["step_kind"]
        for row in explanation["inputs"]:
            assert row["label"] and row["value"]["display"]
            # ``unresolved`` is the honest hole for a command with no contract
            # in the registry; every other row keeps its canonical payload.
            assert (
                row["value"]["canonical"] is not None
                or row["value"]["kind"] in {"redacted", "unresolved"}
            )
        # Every outcome the inspector lists is an edge the canvas can draw.
        drawn = {
            edge["outcome"] for edge in graph["edges"] if edge["source"] == node["id"]
        }
        listed = {
            item["outcome"]
            for item in explanation["outcomes"]
            if item["target_step_id"] is not None
        }
        assert listed == drawn, node["id"]


def test_explanations_stay_deterministic_across_projections():
    first = json.dumps(_project(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(_project(), sort_keys=True, separators=(",", ":"))
    assert first == second
    assert '"effect_summary"' in first


def test_context_and_composite_values_render_readably():
    from src.playbooks.graph_projection import project_value

    assert project_value({"type": "context_ref", "path": "run_id"})["display"] == (
        "this run's run_id"
    )
    assert project_value(
        {
            "type": "coalesce",
            "options": [
                {"type": "event_ref", "path": "note"},
                {"type": "literal", "value": "none"},
            ],
        }
    )["display"] == "this event's note or else none"
    assert project_value(
        {"type": "list", "items": [{"type": "literal", "value": "a"}]}
    )["display"] == "[a]"
    assert project_value(
        {"type": "object", "fields": {"k": {"type": "literal", "value": "v"}}}
    )["display"] == "{k: v}"


def test_boolean_and_exists_conditions_render_readably():
    from src.playbooks.graph_projection import _condition_text

    assert (
        _condition_text(
            {
                "type": "bool",
                "op": "and",
                "operands": [
                    {
                        "type": "comparison",
                        "op": "gte",
                        "left": {"type": "binding_ref", "binding": "gate", "path": "count"},
                        "right": {"type": "literal", "value": 2},
                    },
                    {"type": "exists", "value": {"type": "event_ref", "path": "note"}},
                ],
            }
        )
        == "(gate.count >= 2) and (this event's note is present)"
    )
    assert (
        _condition_text(
            {
                "type": "bool",
                "op": "not",
                "operands": [
                    {
                        "type": "exists",
                        "value": {"type": "loop_ref", "binding": "task"},
                        "mode": "truthy",
                    }
                ],
            }
        )
        == "not (task is truthy)"
    )
