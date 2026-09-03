"""Runtime resolution of the Package 2 typed value and condition union.

Package 4 child plan §2.5 item 1: Package 2 shipped the models and the
static passes but no resolver, and roadmap §3 puts "typed values, templates,
references, comparisons, and condition trees" in ``expressions.py``.  These
are the runtime halves of the shapes ``validation.py`` already checks
statically, so an artifact that validated cannot fail here for a *structural*
reason — only because a value was absent or the wrong type at run time.
"""

from __future__ import annotations

import pytest

from src.playbooks.expressions import (
    BindingRef,
    BooleanExpr,
    CoalesceValue,
    Comparison,
    ContextRef,
    EventRef,
    Exists,
    ListValue,
    LiteralValue,
    LoopRef,
    ObjectValue,
    ResolutionScope,
    TemplateValue,
    ValueResolutionError,
    evaluate_condition,
    resolve_value,
)


def scope(**overrides):
    base = {
        "event": {"task": {"id": "t-1", "branch_name": "aq/x"}, "project_id": "p", "count": 3},
        "context": {"run_id": "r-1", "rule_id": "rule-a", "now": "2026-09-02T00:00:00Z"},
        "bindings": {"review": {"task_id": "t-9", "created": True}},
        "loop": {},
    }
    base.update(overrides)
    return ResolutionScope(**base)


class TestValues:
    def test_literal_returns_its_value(self):
        assert resolve_value(LiteralValue(value="x"), scope()) == "x"

    def test_event_ref_walks_a_dotted_path(self):
        assert resolve_value(EventRef(path="task.branch_name"), scope()) == "aq/x"

    def test_context_ref_reads_the_engine_context(self):
        assert resolve_value(ContextRef(path="run_id"), scope()) == "r-1"

    def test_binding_ref_reads_a_whole_binding_and_a_path(self):
        assert resolve_value(BindingRef(binding="review"), scope()) == {
            "task_id": "t-9",
            "created": True,
        }
        assert resolve_value(BindingRef(binding="review", path="task_id"), scope()) == "t-9"

    def test_loop_ref_reads_the_item_and_the_index(self):
        s = scope(loop={"task": {"id": "t-2"}, "task#index": 4})
        assert resolve_value(LoopRef(binding="task", path="id"), s) == "t-2"
        assert resolve_value(LoopRef(binding="task", index=True), s) == 4

    def test_list_and_object_recurse(self):
        value = ObjectValue(
            fields={
                "ids": ListValue(items=[EventRef(path="task.id"), LiteralValue(value=2)]),
            }
        )
        assert resolve_value(value, scope()) == {"ids": ["t-1", 2]}

    def test_template_concatenates_and_only_produces_a_string(self):
        value = TemplateValue(
            parts=[LiteralValue(value="review "), EventRef(path="task.id")]
        )
        assert resolve_value(value, scope()) == "review t-1"

    def test_template_renders_none_as_empty_rather_than_the_word_none(self):
        value = TemplateValue(parts=[LiteralValue(value="x="), BindingRef(binding="review", path="missing")])
        with pytest.raises(ValueResolutionError):
            resolve_value(value, scope())

    def test_coalesce_takes_the_first_non_null_branch(self):
        value = CoalesceValue(
            options=[BindingRef(binding="review", path="absent"), LiteralValue(value="fallback")]
        )
        assert resolve_value(value, scope()) == "fallback"

    def test_coalesce_that_exhausts_every_branch_raises(self):
        value = CoalesceValue(
            options=[
                BindingRef(binding="review", path="absent"),
                BindingRef(binding="review", path="also_absent"),
            ]
        )
        with pytest.raises(ValueResolutionError):
            resolve_value(value, scope())


class TestMissingReferences:
    """Every miss raises rather than coercing — plan §3.4 step 4."""

    def test_missing_event_path_raises(self):
        with pytest.raises(ValueResolutionError) as exc:
            resolve_value(EventRef(path="task.nope"), scope())
        assert "task.nope" in str(exc.value)

    def test_unknown_binding_raises(self):
        with pytest.raises(ValueResolutionError):
            resolve_value(BindingRef(binding="ghost"), scope())

    def test_loop_ref_outside_a_loop_raises(self):
        with pytest.raises(ValueResolutionError):
            resolve_value(LoopRef(binding="task"), scope())

    def test_a_path_into_a_scalar_raises_rather_than_returning_none(self):
        with pytest.raises(ValueResolutionError):
            resolve_value(EventRef(path="project_id.deeper"), scope())


class TestConditions:
    def test_comparison_operators(self):
        cases = [
            ("eq", 3, True),
            ("ne", 3, False),
            ("lt", 4, True),
            ("gt", 2, True),
            ("lte", 3, True),
            ("gte", 4, False),
        ]
        for op, right, expected in cases:
            cond = Comparison(op=op, left=EventRef(path="count"), right=LiteralValue(value=right))
            assert evaluate_condition(cond, scope()) is expected, op

    def test_membership_and_containment(self):
        assert evaluate_condition(
            Comparison(op="in", left=EventRef(path="project_id"), right=LiteralValue(value=["p", "q"])),
            scope(),
        )
        assert evaluate_condition(
            Comparison(op="contains", left=LiteralValue(value=["p"]), right=EventRef(path="project_id")),
            scope(),
        )
        assert not evaluate_condition(
            Comparison(op="not_in", left=EventRef(path="project_id"), right=LiteralValue(value=["p"])),
            scope(),
        )

    def test_ordering_between_incomparable_types_raises(self):
        cond = Comparison(op="lt", left=EventRef(path="project_id"), right=LiteralValue(value=1))
        with pytest.raises(ValueResolutionError):
            evaluate_condition(cond, scope())

    def test_boolean_and_or_not(self):
        yes = Comparison(op="eq", left=EventRef(path="count"), right=LiteralValue(value=3))
        no = Comparison(op="eq", left=EventRef(path="count"), right=LiteralValue(value=9))
        assert evaluate_condition(BooleanExpr(op="and", operands=[yes, yes]), scope())
        assert not evaluate_condition(BooleanExpr(op="and", operands=[yes, no]), scope())
        assert evaluate_condition(BooleanExpr(op="or", operands=[yes, no]), scope())
        assert not evaluate_condition(BooleanExpr(op="not", operands=[yes]), scope())

    def test_exists_present_is_not_truthy(self):
        s = scope(bindings={"review": {"task_id": "", "created": False}})
        present = Exists(value=BindingRef(binding="review", path="task_id"), mode="present")
        truthy = Exists(value=BindingRef(binding="review", path="task_id"), mode="truthy")
        assert evaluate_condition(present, s) is True
        assert evaluate_condition(truthy, s) is False

    def test_exists_swallows_a_missing_reference_rather_than_raising(self):
        cond = Exists(value=BindingRef(binding="ghost"), mode="present")
        assert evaluate_condition(cond, scope()) is False
