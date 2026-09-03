"""§4.2 / §4.3 / §6.5 — the typed value union, conditions, and the type lattice.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from src.playbooks import expressions as E
from src.playbooks.expressions import (
    ENGINE_CONTEXT_SCHEMA,
    BindingRef,
    CoalesceValue,
    Comparison,
    Condition,
    ContextRef,
    EventRef,
    Exists,
    ListValue,
    LiteralValue,
    LoopRef,
    ObjectValue,
    TemplateValue,
    Value,
    condition_values,
    expression_depth,
    walk_value,
)
from src.playbooks.validation import (
    UNKNOWN,
    ValueType,
    _build_scope,
    _Context,
    _value_type,
    event_field_type,
    join_types,
    literal_type,
    type_from_annotation,
    type_from_schema,
    walk_type_path,
)
from tests.playbook_v2_helpers import (
    GOLDEN,
    StubContracts,
    StubEvents,
    StubProfiles,
)

VALUES = TypeAdapter(Value)
CONDITIONS = TypeAdapter(Condition)


class TestValueUnion:
    """§4.2 — the nine discriminator strings are the wire contract."""

    def test_the_union_has_exactly_nine_members(self):
        assert set(E.ValueKind.__args__) == {
            "literal",
            "event_ref",
            "context_ref",
            "binding_ref",
            "loop_ref",
            "list",
            "object",
            "template",
            "coalesce",
        }

    @pytest.mark.parametrize(
        "payload,model",
        [
            ({"type": "literal", "value": 3}, LiteralValue),
            ({"type": "event_ref", "path": "task.branch_name"}, EventRef),
            ({"type": "context_ref", "path": "run_id"}, ContextRef),
            ({"type": "binding_ref", "binding": "review", "path": "task_id"}, BindingRef),
            ({"type": "loop_ref", "binding": "item", "index": True}, LoopRef),
            ({"type": "list", "items": [{"type": "literal", "value": 1}]}, ListValue),
            (
                {"type": "object", "fields": {"a": {"type": "literal", "value": 1}}},
                ObjectValue,
            ),
            ({"type": "template", "parts": [{"type": "literal", "value": "x"}]}, TemplateValue),
            (
                {
                    "type": "coalesce",
                    "options": [
                        {"type": "event_ref", "path": "note"},
                        {"type": "literal", "value": "fallback"},
                    ],
                },
                CoalesceValue,
            ),
        ],
    )
    def test_every_kind_discriminates(self, payload, model):
        assert isinstance(VALUES.validate_python(payload), model)

    def test_an_unrecognised_kind_fails_discrimination(self):
        with pytest.raises(ValidationError):
            VALUES.validate_python({"type": "shell", "cmd": "rm -rf /"})

    def test_a_coalesce_needs_at_least_two_options(self):
        with pytest.raises(ValidationError):
            VALUES.validate_python(
                {"type": "coalesce", "options": [{"type": "literal", "value": 1}]}
            )

    def test_a_template_has_no_format_language(self):
        """§10.4 — parts are typed values, never a format string."""
        assert "parts" in TemplateValue.model_fields
        assert set(TemplateValue.model_fields) == {"type", "parts"}

    @pytest.mark.parametrize("bad", ["a..b", ".lead", "trail.", "has space", "1st"])
    def test_a_dotted_path_must_be_well_formed(self, bad):
        with pytest.raises(ValidationError):
            VALUES.validate_python({"type": "event_ref", "path": bad})

    def test_walk_value_is_pre_order_and_total(self):
        nested = VALUES.validate_python(
            {
                "type": "template",
                "parts": [
                    {"type": "literal", "value": "a"},
                    {
                        "type": "coalesce",
                        "options": [
                            {"type": "event_ref", "path": "note"},
                            {"type": "literal", "value": "b"},
                        ],
                    },
                ],
            }
        )
        kinds = [node.type for node in walk_value(nested)]
        assert kinds == ["template", "literal", "coalesce", "event_ref", "literal"]

    def test_expression_depth_counts_nesting(self):
        leaf = LiteralValue(value=1)
        assert expression_depth(leaf) == 1
        assert expression_depth(ListValue(items=[ListValue(items=[leaf])])) == 3


class TestConditions:
    """§4.3 — a separate union, and no vacuous truth."""

    @pytest.mark.parametrize(
        "payload,model",
        [
            (
                {
                    "type": "comparison",
                    "op": "eq",
                    "left": {"type": "literal", "value": 1},
                    "right": {"type": "literal", "value": 1},
                },
                Comparison,
            ),
            ({"type": "exists", "value": {"type": "event_ref", "path": "note"}}, Exists),
        ],
    )
    def test_condition_kinds(self, payload, model):
        assert isinstance(CONDITIONS.validate_python(payload), model)

    @pytest.mark.parametrize("op", ["and", "or"])
    def test_a_nullary_boolean_is_rejected(self, op):
        """Closes ``conditions.py``'s ``all: [] -> True`` / ``any: [] -> False``."""
        with pytest.raises(ValidationError, match="empty_boolean_operand"):
            CONDITIONS.validate_python({"type": "bool", "op": op, "operands": []})

    @pytest.mark.parametrize("op", ["and", "or"])
    def test_a_unary_and_or_is_rejected(self, op):
        with pytest.raises(ValidationError, match="empty_boolean_operand"):
            CONDITIONS.validate_python(
                {
                    "type": "bool",
                    "op": op,
                    "operands": [{"type": "exists", "value": {"type": "literal", "value": 1}}],
                }
            )

    def test_not_takes_exactly_one_operand(self):
        exists = {"type": "exists", "value": {"type": "literal", "value": 1}}
        CONDITIONS.validate_python({"type": "bool", "op": "not", "operands": [exists]})
        with pytest.raises(ValidationError, match="empty_boolean_operand"):
            CONDITIONS.validate_python({"type": "bool", "op": "not", "operands": [exists, exists]})

    def test_quantifiers_are_not_part_of_initial_v2(self):
        """§4.3 / §20 item 1 — ``any``/``all`` over a collection is excluded."""
        assert set(E.BooleanOp.__args__) == {"and", "or", "not"}
        with pytest.raises(ValidationError):
            CONDITIONS.validate_python(
                {"type": "quantifier", "op": "any", "over": {"type": "literal", "value": []}}
            )

    def test_condition_values_reaches_every_leaf(self):
        condition = CONDITIONS.validate_python(
            {
                "type": "bool",
                "op": "and",
                "operands": [
                    {
                        "type": "comparison",
                        "op": "eq",
                        "left": {"type": "binding_ref", "binding": "gate"},
                        "right": {"type": "literal", "value": False},
                    },
                    {"type": "exists", "value": {"type": "event_ref", "path": "title"}},
                ],
            }
        )
        kinds = sorted(node.type for node in condition_values(condition))
        assert kinds == ["binding_ref", "event_ref", "literal"]


class TestEngineContext:
    """§4.2.1 — a closed schema; there is no dynamic context."""

    def test_the_schema_is_the_eight_declared_paths(self):
        assert set(ENGINE_CONTEXT_SCHEMA) == {
            "run_id",
            "dispatch_id",
            "playbook_id",
            "rule_id",
            "artifact_sha256",
            "now",
            "attempt",
            "iteration_index",
        }

    def test_iteration_index_is_loop_scoped(self):
        assert E.LOOP_ONLY_CONTEXT_PATHS == frozenset({"iteration_index"})


def test_expressions_module_has_no_intra_package_imports():
    """§20 item 8 — Package 7 moves ``parse_json_from_text`` in here later.

    That move stays cheap only while this module has no edge into
    ``src.playbooks``.
    """
    tree = ast.parse(Path(E.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert [name for name in imported if name.startswith("src.")] == []
    # ``collections.abc`` joined the set with Package 4's runtime resolver
    # (its child plan §2.5 item 1): ``ResolutionScope`` is typed over
    # ``Mapping``.  Still stdlib-only, so the property the assertion exists
    # for — no edge into ``src.playbooks`` — is untouched.
    assert set(imported) <= {
        "__future__",
        "collections.abc",
        "typing",
        "pydantic",
        "json",
        "re",
    }


class TestTypeLattice:
    """§6.5 — one test per row of the static-type table."""

    @pytest.fixture
    def scope(self):
        from src.playbooks.definition import load_definition_json

        definition = load_definition_json(GOLDEN.read_text())
        context = _Context(
            definition=definition,
            contracts=StubContracts(),
            profiles=StubProfiles(),
            events=StubEvents(),
            inventory=None,
        )
        rule = definition.rules[0]
        context.closures[rule.id] = {
            step_id for step_id, step in definition.steps.items() if step.rule == rule.id
        }
        context.producers[rule.id] = {
            step.save_result_as: step_id
            for step_id, step in definition.steps.items()
            if step.rule == rule.id and getattr(step, "save_result_as", None)
        }
        return context, _build_scope(context, rule)

    def _type(self, scope, payload):
        context, type_scope = scope
        return _value_type(context, type_scope, VALUES.validate_python(payload))

    def test_literal_takes_the_json_type_of_its_value(self, scope):
        assert self._type(scope, {"type": "literal", "value": "x"}).kind == "string"
        assert self._type(scope, {"type": "literal", "value": 1}).kind == "integer"
        assert self._type(scope, {"type": "literal", "value": True}).kind == "boolean"
        assert self._type(scope, {"type": "literal", "value": None}).kind == "null"
        assert self._type(scope, {"type": "literal", "value": [1, 2]}).kind == "array"

    def test_event_ref_reads_the_registered_event_schema(self, scope):
        assert self._type(scope, {"type": "event_ref", "path": "title"}).kind == "string"
        nested = self._type(scope, {"type": "event_ref", "path": "task.branch_name"})
        assert nested.kind == "string"

    def test_context_ref_reads_the_engine_schema(self, scope):
        assert self._type(scope, {"type": "context_ref", "path": "run_id"}).kind == "string"
        assert self._type(scope, {"type": "context_ref", "path": "attempt"}).kind == "integer"
        assert self._type(scope, {"type": "context_ref", "path": "nope"}).is_unknown

    def test_binding_ref_reads_the_producing_step_result(self, scope):
        assert self._type(
            scope, {"type": "binding_ref", "binding": "approval", "path": "resolution"}
        ).kind == "string"
        assert self._type(
            scope, {"type": "binding_ref", "binding": "escalation", "path": "task_id"}
        ).kind == "string"
        assert self._type(scope, {"type": "binding_ref", "binding": "risk"}).kind == "object"

    def test_loop_ref_is_the_index_or_the_item(self, scope):
        assert self._type(
            scope, {"type": "loop_ref", "binding": "task", "index": True}
        ).kind == "integer"
        assert self._type(scope, {"type": "loop_ref", "binding": "task"}).is_unknown

    def test_list_joins_its_item_types(self, scope):
        listed = self._type(
            scope,
            {
                "type": "list",
                "items": [{"type": "literal", "value": 1}, {"type": "literal", "value": 2}],
            },
        )
        assert listed.kind == "array"
        assert listed.item_type.kind == "integer"

    def test_object_carries_known_properties(self, scope):
        obj = self._type(
            scope, {"type": "object", "fields": {"a": {"type": "literal", "value": "s"}}}
        )
        assert obj.kind == "object"
        assert obj.properties["a"].kind == "string"

    def test_template_is_always_a_string(self, scope):
        assert self._type(
            scope,
            {"type": "template", "parts": [{"type": "literal", "value": 1}]},
        ).kind == "string"

    def test_coalesce_joins_and_drops_null_when_the_last_option_is_total(self, scope):
        total = self._type(
            scope,
            {
                "type": "coalesce",
                "options": [
                    {"type": "event_ref", "path": "title"},
                    {"type": "literal", "value": "fallback"},
                ],
            },
        )
        assert total.kind == "string" and total.nullable is False
        partial = self._type(
            scope,
            {
                "type": "coalesce",
                "options": [
                    {"type": "event_ref", "path": "title"},
                    {"type": "literal", "value": None},
                ],
            },
        )
        assert partial.nullable is True

    def test_unknown_is_compatible_with_everything(self):
        assert UNKNOWN.compatible_with(ValueType("string"))
        assert ValueType("string").compatible_with(UNKNOWN)
        assert not ValueType("string").compatible_with(ValueType("integer"))

    def test_an_integer_satisfies_a_number(self):
        assert ValueType("integer").compatible_with(ValueType("number"))
        assert not ValueType("number").compatible_with(ValueType("integer"))


class TestSchemaAndAnnotationTypes:
    """The two entry points into the lattice."""

    def test_type_from_schema_reads_nullable_unions(self):
        assert type_from_schema({"type": ["string", "null"]}) == ValueType("string", nullable=True)
        assert type_from_schema({"type": "array", "items": {"type": "integer"}}).item_type.kind == (
            "integer"
        )
        assert type_from_schema({"enum": ["a", "b"]}).kind == "string"
        assert type_from_schema({}).is_unknown

    def test_type_from_annotation_covers_the_pydantic_shapes(self):
        assert type_from_annotation(str) == ValueType("string")
        assert type_from_annotation(str | None) == ValueType("string", nullable=True)
        assert type_from_annotation(list[str]).item_type.kind == "string"
        assert type_from_annotation(bool).kind == "boolean"

        class Nested(BaseModel):
            name: str

        assert type_from_annotation(Nested).kind == "object"

    def test_join_types_widens_or_gives_up(self):
        assert join_types([ValueType("integer"), ValueType("number")]).kind == "number"
        assert join_types([ValueType("string"), ValueType("integer")]).is_unknown
        assert join_types([ValueType("string"), UNKNOWN]).kind == "string"
        assert join_types([]).is_unknown

    def test_walk_type_path_stops_at_the_first_unknown(self):
        root = ValueType("object", properties={"a": ValueType("object", properties={"b": ValueType("string")})})
        assert walk_type_path(root, "a.b").kind == "string"
        assert walk_type_path(root, "a.z").is_unknown
        assert walk_type_path(ValueType("string"), "a").is_unknown

    def test_event_field_type_distinguishes_undeclared_from_untyped(self):
        schema = {
            "required": ["project_id"],
            "optional": ["legacy"],
            "fields": {"project_id": {"type": "string"}},
        }
        assert event_field_type(schema, "project_id").kind == "string"
        assert event_field_type(schema, "legacy") == UNKNOWN
        assert event_field_type(schema, "nope") is None

    def test_literal_type_reads_python_values(self):
        assert literal_type(True).kind == "boolean"
        assert literal_type(1).kind == "integer"
        assert literal_type(1.5).kind == "number"
        assert literal_type({"a": 1}).properties["a"].kind == "integer"
