"""Bounded lexical collection expressions, their consumers and review evidence."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from src.api.models.playbook_v2 import ExplanationValueDTO
from src.playbooks import expressions as E
from src.playbooks.artifact_diff import _fields
from src.playbooks.definition import DecisionStep, PlaybookDefinition, step_values
from src.playbooks.engine import PlaybookEngine
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.executors.base import EngineServices, ExecutionMode
from src.playbooks.executors.decision import DecisionExecutor
from src.playbooks.explanation import _value
from src.playbooks.graph_projection import _condition_text, project_graph, project_value
from src.playbooks.integration_policy import _check_reads
from src.integration.runtime_contracts import SubjectKind
from src.playbooks.semantic_diff import diff_definitions
from src.playbooks.validation import (
    _Context,
    _TypeScope,
    _value_type,
    validate_definition,
)
from tests.fixtures.contracts.engine_contracts import ENSURE_TASK, registry_with
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
    minimal_artifact,
)
from tests.playbook_v2_helpers import (
    StubContracts,
    StubEvents,
    StubInventory,
    StubProfiles,
    check,
    codes,
    twin,
)


def lit(value):
    return E.LiteralValue(value=value)


def ref(name="item", path=None):
    return E.LoopRef(binding=name, path=path)


def mapped(collection=None, value=None, name="item"):
    return E.MapValue(
        collection=collection if collection is not None else lit([1, 2]),
        item_binding=name,
        value=value if value is not None else ref(name),
    )


def filtered(collection=None):
    return E.FilterValue(
        collection=collection if collection is not None else lit([1, 2, 3]),
        item_binding="item",
        predicate=E.Comparison(op="gt", left=ref(), right=lit(1)),
    )


@pytest.mark.parametrize(
    "value",
    [
        mapped(),
        filtered(),
        E.LenValue(collection=lit([])),
        E.DiffValue(left=lit([]), right=lit([]), key_path="id"),
    ],
)
def test_wire_roundtrip_and_unknown_fields(value):
    adapter = TypeAdapter(E.Value)
    assert adapter.validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        adapter.validate_python({**value.model_dump(), "unexpected": 1})


def test_nested_shadowing_collection_uses_outer_scope_and_bindings_are_unchanged():
    scope = E.ResolutionScope(bindings={"item": "step"}).with_loop_item("item", [1, 2], 9)
    expression = mapped(
        collection=ref(),
        value=mapped(
            collection=E.ListValue(items=[ref()]),
            value=E.ObjectValue(
                fields={
                    "value": ref(),
                    "index": E.LoopRef(binding="item", index=True),
                    "step": E.BindingRef(binding="item"),
                }
            ),
        ),
    )
    assert E.resolve_value(expression, scope) == [
        [{"value": 1, "index": 0, "step": "step"}],
        [{"value": 2, "index": 0, "step": "step"}],
    ]
    assert scope.loop == {"item": [1, 2], "item#index": 9}
    assert scope.bindings == {"item": "step"}
    assert E.resolve_value(E.LenValue(collection=filtered()), scope) == 2


def test_traversal_carries_scope_per_occurrence_and_visits_nested_predicates_once():
    shared = ref()
    value = mapped(collection=shared, value=E.LenValue(collection=filtered(shared)))
    walked = E.walk_value(value, scoped=True)
    occurrences = [scope for node, scope in walked if node is shared]
    assert occurrences == [(), (value,)]
    assert len(E.walk_condition(value)) == 1
    condition = E.Exists(value=value)
    assert E.condition_values(condition) == E.walk_value(value)
    assert E.expression_depth(condition) == 6


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ([2, 1, 2, 3, 1], [3], [2, 1]),
        ([True, 1, False, 0, 1.0], [], [True, 1, False, 0]),
        ([1, 1.0, 2, True], [1.0], [2, True]),
        ([{"b": 2, "a": 1}, {"a": 1, "b": 2}, [1, 2]], [{"a": 1.0, "b": 2.0}], [[1, 2]]),
        ([None, False, None], [False], [None]),
    ],
)
def test_diff_canonical_stable_first_occurrence(left, right, expected):
    expression = E.DiffValue(left=E.BindingRef(binding="left"), right=E.BindingRef(binding="right"))
    result = E.resolve_value(expression, E.ResolutionScope(bindings={"left": left, "right": right}))
    # Type-sensitive assertion: Python list equality alone conflates booleans/numbers.
    assert [(type(item), item) for item in result] == [(type(item), item) for item in expected]


def test_diff_key_path_keeps_first_left_record_and_checks_all_right_keys():
    left = [{"meta": {"id": 2}, "text": "first"}, {"meta": {"id": 2}}, {"meta": {"id": 1}}]
    right = [{"meta": {"id": 1.0}}]
    expression = E.DiffValue(
        left=E.BindingRef(binding="l"), right=E.BindingRef(binding="r"), key_path="meta.id"
    )
    scope = E.ResolutionScope(bindings={"l": left, "r": right})
    assert E.resolve_value(expression, scope) == [left[0]]
    for invalid in ({}, {"meta": {"id": []}}, {"meta": {"id": {}}}):
        with pytest.raises(E.ValueResolutionError):
            E.resolve_value(expression, E.ResolutionScope(bindings={"l": [], "r": [invalid]}))


@pytest.mark.parametrize(
    "value",
    [
        mapped(collection=lit(None)),
        filtered(lit(None)),
        E.LenValue(collection=lit(None)),
        E.DiffValue(left=lit(None), right=lit([])),
        E.DiffValue(left=lit([]), right=lit(None)),
        E.LenValue(collection=lit("abc")),
        mapped(collection=lit([1]), value=ref(path="missing")),
    ],
)
def test_null_wrong_types_and_missing_paths_fail(value):
    with pytest.raises(E.ValueResolutionError):
        E.resolve_value(value, E.ResolutionScope())


def test_explicit_coalesce_and_dynamic_object_mapping():
    coalesced = E.CoalesceValue(options=[E.BindingRef(binding="absent"), lit([])])
    assert E.resolve_value(E.LenValue(collection=coalesced), E.ResolutionScope()) == 0
    expression = mapped(E.BindingRef(binding="rows"), ref(path="id"))
    assert E.resolve_value(expression, E.ResolutionScope(bindings={"rows": [{"id": "one"}]})) == [
        "one"
    ]
    with pytest.raises(E.ValueResolutionError):
        E.resolve_value(expression, E.ResolutionScope(bindings={"rows": [{}]}))


@pytest.mark.parametrize(
    "expression,budget,code",
    [
        (mapped(), E.EvaluationBudget(max_visits=5), "expression_visit_limit"),
        (
            E.LenValue(collection=lit([1, 2])),
            E.EvaluationBudget(max_items=1),
            "expression_item_limit",
        ),
        (mapped(value=lit("abcdefgh")), E.EvaluationBudget(max_bytes=15), "expression_byte_limit"),
        (E.LenValue(collection=lit([])), E.EvaluationBudget(max_depth=1), "expression_depth_limit"),
    ],
)
def test_named_budget_errors_cannot_be_swallowed_by_exists(expression, budget, code):
    with pytest.raises(E.ExpressionBudgetExceeded, match=code):
        E.evaluate_condition(E.Exists(value=expression), E.ResolutionScope(), budget)


def test_byte_limit_applies_before_second_map_body_and_counts_utf8_escaping():
    expression = mapped(value=lit("\\\né" * 4))
    budget = E.EvaluationBudget(max_bytes=26)
    with pytest.raises(E.ExpressionBudgetExceeded, match="expression_byte_limit"):
        E.resolve_value(expression, E.ResolutionScope(), budget)
    # Large strings fail before serialization of the whole value.
    with pytest.raises(E.ExpressionBudgetExceeded, match="expression_byte_limit"):
        E.resolve_value(
            E.BindingRef(binding="x"), E.ResolutionScope(bindings={"x": ["x" * 300_000]})
        )


def test_shared_budget_across_calls_and_no_reset_in_coalesce():
    budget = E.EvaluationBudget(max_visits=3)
    assert E.resolve_value(lit(1), E.ResolutionScope(), budget) == 1
    with pytest.raises(E.ExpressionBudgetExceeded, match="expression_visit_limit"):
        E.resolve_value(lit(2), E.ResolutionScope(), budget)
    with pytest.raises(E.ExpressionBudgetExceeded, match="expression_item_limit"):
        E.resolve_value(
            E.CoalesceValue(options=[lit([1, 2]), lit([])]),
            E.ResolutionScope(),
            E.EvaluationBudget(max_items=1),
        )


def test_production_limits_and_flat_linear_diff():
    with pytest.raises(E.ExpressionBudgetExceeded, match="expression_item_limit"):
        E.resolve_value(E.LenValue(collection=lit([0] * 10_001)), E.ResolutionScope())
    values = list(range(4000))
    budget = E.EvaluationBudget()
    result = E.resolve_value(
        E.DiffValue(left=lit(values), right=lit(values[2000:])), E.ResolutionScope(), budget
    )
    assert result == values[:2000]
    assert budget.visits < 30_000


def with_expression(value):
    payload = twin()
    payload["steps"]["act"]["inputs"]["note"] = value.model_dump(mode="json")
    return PlaybookDefinition.model_validate(payload)


def test_validation_allows_local_refs_but_rejects_free_refs_and_binding_reads():
    good = with_expression(E.TemplateValue(parts=[mapped()]))
    assert "loop_ref_outside_loop" not in codes(check(good))
    bad = with_expression(mapped(collection=ref()))
    assert "loop_ref_outside_loop" in codes(check(bad))
    bad = with_expression(mapped(value=E.BindingRef(binding="not_assigned")))
    assert "binding_not_definitely_assigned" in codes(check(bad))
    assert any(isinstance(node, E.LoopRef) for node in step_values(good.steps["act"]))


def test_types_infer_map_items_and_filter_diff_preserve_known_item_type():
    definition = with_expression(mapped())
    context = _Context(definition, StubContracts(), StubProfiles(), StubEvents(), None)
    scope = _TypeScope(definition.rules[0], None, {}, {})
    for expression in (mapped(), filtered(), E.DiffValue(left=lit([1]), right=lit([]))):
        kind = _value_type(context, scope, expression)
        assert kind.kind == "array"
        assert kind.item_type.kind == "integer"
    expression = mapped(E.ListValue(items=[E.ObjectValue(fields={"id": lit("x")})]), ref(path="id"))
    assert _value_type(context, scope, expression).item_type.kind == "string"
    assert "type_mismatch" in codes(check(with_expression(E.LenValue(collection=lit(1)))))


def test_source_inventory_includes_expression_binding_and_key_path():
    definition = with_expression(
        E.DiffValue(left=mapped(name="authored"), right=lit([]), key_path="identity")
    )
    diagnostics = validate_definition(
        definition,
        contracts=StubContracts(),
        profiles=StubProfiles(),
        events=StubEvents(),
        inventory=StubInventory(set()),
    )
    messages = [d.message for d in diagnostics if d.code == "unknown_identifier"]
    assert any("'authored'" in message for message in messages)
    assert any("'identity'" in message for message in messages)


def test_complete_review_text_and_dto_for_all_transforms():
    expression = E.DiffValue(left=mapped(filtered(), ref()), right=lit([]), key_path="identity")
    projected = project_value(expression)
    assert projected["kind"] == "diff"
    assert (
        projected["display"]
        == 'diff(map(filter([1,2,3], item => item > 1), item => item), [], key_path="identity")'
    )
    assert projected["canonical"] == expression.model_dump(mode="json")
    assert ExplanationValueDTO.model_validate(projected).kind == "diff"
    assert (
        _value(expression.model_dump(), event_type=None, loop_name=None).text
        == projected["display"]
    )
    assert "len(" in _condition_text(
        E.Comparison(op="gt", left=E.LenValue(collection=expression), right=lit(0))
    )
    assert project_value(E.LoopRef(binding="item", index=True))["display"] == "item#index"
    assert project_value(expression, redacted=True)["canonical"] is None


def test_expression_predicate_and_key_changes_are_executable_diff():
    base = with_expression(E.TemplateValue(parts=[filtered()]))
    target = with_expression(E.TemplateValue(parts=[replace_predicate(filtered())]))
    changes = diff_definitions(base, target)
    assert changes.executable_change
    assert any(
        field.pointer.endswith("/predicate/right/value") and field.executable
        for step in changes.steps
        for field in step.fields
    )
    fields = _fields(filtered().model_dump(), replace_predicate(filtered()).model_dump())
    assert fields[0]["executable"]
    assert _fields({"value": True}, {"value": 1})  # JSON identities, not Python equality
    assert _fields({"key_path": "id"}, {"key_path": "name"})[0]["executable"]


def replace_predicate(value):
    return value.model_copy(
        update={"predicate": value.predicate.model_copy(update={"right": lit(2)})}
    )


async def test_decision_cases_share_budget_and_report_input_failure():
    step = DecisionStep.model_validate(
        {
            "rule": "r",
            "title": "Choose",
            "source": {"path": "x", "start_line": 1, "end_line": 1},
            "cases": [
                {
                    "when": {
                        "type": "comparison",
                        "op": "eq",
                        "left": {"type": "literal", "value": 1},
                        "right": {"type": "literal", "value": 2},
                    },
                    "goto": "yes",
                }
            ]
            * 2,
            "default": "no",
        }
    )
    ctx = SimpleNamespace(
        scope=E.ResolutionScope(),
        evaluation_budget=E.EvaluationBudget(max_visits=6),
        step_id="choose",
    )
    result = await DecisionExecutor().execute(step, ctx)
    assert result.outcome == "input_resolution_failed"
    assert "expression_visit_limit" in result.diagnostics[0]


@pytest.mark.parametrize("mode", [ExecutionMode.LIVE, ExecutionMode.DRY_RUN])
async def test_engine_input_set_budget_prevents_dispatch(monkeypatch, mode):
    import src.playbooks.engine as engine_module

    monkeypatch.setattr(engine_module, "EvaluationBudget", lambda: E.EvaluationBudget(max_visits=3))
    artifact = minimal_artifact()
    step = artifact.steps["ensure-review-task"].model_copy(
        update={
            "inputs": {"project_id": lit("p"), "title": lit("t")},
            "transitions": {"input_resolution_failed": "bad", "created": "done", "reused": "done"},
        }
    )
    artifact = artifact.model_copy(update={"steps": {**artifact.steps, "ensure-review-task": step}})
    ref_artifact = artifact_ref_for(artifact)
    registry, adapter = registry_with(ENSURE_TASK)
    runs = RecordingRunRepository()
    store = InMemoryArtifactStore()
    store.put(artifact)
    engine = PlaybookEngine(
        services=EngineServices(contracts=registry, clock=lambda: 1000, artifact_store=store),
        runs=runs,
        activations=StubActivations([ref_artifact]),
    )
    if mode is ExecutionMode.LIVE:
        await engine.run_rule(ref_artifact, "r", {"event_type": "task.completed"}, TRUSTED_LOCAL)
        assert any(receipt.error_code == "input_resolution_failed" for receipt in runs.receipts)
        assert any("expression_visit_limit" in (receipt.error or "") for receipt in runs.receipts)
    else:
        tree = await engine.dry_run(ref_artifact, {"event_type": "task.completed"}, TRUSTED_LOCAL)
        assert "expression_visit_limit" in str(tree)
    assert adapter.calls == []


def test_guards_use_the_same_lexical_scope_type_and_source_checks():
    definition = with_expression(lit("note"))
    rule = definition.rules[0].model_copy(update={"guard": E.Exists(value=mapped())})
    definition = definition.model_copy(update={"rules": [rule]})
    assert "loop_ref_outside_loop" not in codes(check(definition))
    invalid = rule.model_copy(update={"guard": E.Exists(value=mapped(collection=ref()))})
    assert "loop_ref_outside_loop" in codes(
        check(definition.model_copy(update={"rules": [invalid]}))
    )
    invalid = rule.model_copy(update={"guard": E.Exists(value=E.LenValue(collection=lit(1)))})
    assert "type_mismatch" in codes(check(definition.model_copy(update={"rules": [invalid]})))
    diagnostics = validate_definition(
        definition,
        contracts=StubContracts(),
        profiles=StubProfiles(),
        events=StubEvents(),
        inventory=StubInventory(set()),
    )
    assert any(d.code == "unknown_identifier" and "'item'" in d.message for d in diagnostics)


COLLECTION_FIXTURE = Path(__file__).resolve().parents[1] / (
    "dashboard/src/pages/playbook-graph-v2/__tests__/collections.fixture.json"
)


def collection_graph_node():
    """Backend-projected review inputs, shared with the dashboard component tests."""
    definition = minimal_artifact()
    expressions = {
        "filter": filtered(),
        "map": mapped(filtered(), E.LoopRef(binding="item", index=True)),
        "len": E.LenValue(collection=filtered()),
        "diff": E.DiffValue(
            left=E.BindingRef(binding="left"),
            right=E.BindingRef(binding="right"),
            key_path="meta.id",
        ),
    }
    step = definition.steps["ensure-review-task"].model_copy(update={"inputs": expressions})
    definition = definition.model_copy(
        update={
            "steps": {**definition.steps, "ensure-review-task": step},
        }
    )
    graph = project_graph(
        definition,
        artifact_ref_for(definition),
        None,
        contracts=StubContracts(),
        profiles=StubProfiles(),
    )
    return next(node for node in graph["nodes"] if node["id"] == "ensure-review-task")


def test_dashboard_collection_fixture_matches_backend_projection():
    assert COLLECTION_FIXTURE.read_text() == json.dumps(collection_graph_node(), indent=2) + "\n"


def test_integration_policy_permits_only_lexically_bound_item_reads():
    _check_reads(E.walk_value(mapped(), scoped=True), SubjectKind.ROOT_BATCH)
    with pytest.raises(ValueError, match="only s and subject"):
        _check_reads(E.walk_value(mapped(collection=ref()), scoped=True), SubjectKind.ROOT_BATCH)
    # A local name never makes a similarly named step binding available.
    with pytest.raises(ValueError, match="unknown policy binding"):
        _check_reads(
            E.walk_value(mapped(value=E.BindingRef(binding="item")), scoped=True),
            SubjectKind.ROOT_BATCH,
        )


def test_routing_admission_shares_the_step_budget(monkeypatch):
    import src.playbooks.routing as routing

    artifact = minimal_artifact()
    step = artifact.steps["ensure-review-task"].model_copy(
        update={
            "inputs": {"project_id": lit("p"), "title": lit("t")},
        }
    )
    artifact = artifact.model_copy(update={"steps": {**artifact.steps, "ensure-review-task": step}})
    monkeypatch.setattr(routing, "_loaded_activations", lambda *args: [({}, artifact)])
    monkeypatch.setattr(routing, "_trigger_matches", lambda *args, **kwargs: True)
    monkeypatch.setattr(routing, "EvaluationBudget", lambda: E.EvaluationBudget(max_visits=3))
    with pytest.raises(routing._RoutingArtifactUnavailable) as failure:
        list(routing._artifact_command_effects(None, {"project_id": "p"}))
    assert isinstance(failure.value.__cause__, E.ExpressionBudgetExceeded)
    assert failure.value.__cause__.code == "expression_visit_limit"


def test_guard_predicate_changes_are_executable_in_proposal_and_artifact_diffs():
    from src.playbooks.artifact_diff import _rule_rows

    base = with_expression(lit("note"))
    rule = base.rules[0].model_copy(update={"guard": E.Exists(value=filtered())})
    base = base.model_copy(update={"rules": [rule]})
    target = base.model_copy(
        update={
            "rules": [
                rule.model_copy(update={"guard": E.Exists(value=replace_predicate(filtered()))}),
            ]
        }
    )
    diff = diff_definitions(base, target)
    assert diff.executable_change
    assert diff.rules[0].change == "modified"
    assert any(
        field.pointer.endswith("/predicate/right/value") and field.executable
        for field in diff.rules[0].fields
    )
    rows = _rule_rows(base, target)
    assert rows[0]["change"] == "modified"
    assert any(
        field["path"].endswith("/predicate/right/value") and field["executable"]
        for field in rows[0]["field_changes"]
    )
