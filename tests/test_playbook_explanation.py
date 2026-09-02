"""Intent is a pure projection of the contract that a node would invoke."""

from __future__ import annotations

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.models import (
    EFFECT_CLAUSE_TYPES,
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandValue,
    CreateClause,
    CreateOrReuseClause,
    EffectSubject,
    ExecutionContract,
    IdempotencySpec,
    LinkClause,
    OutcomeClass,
    OutcomeSpec,
    ResolveClause,
    ReuseClause,
    SideEffectClass,
    UpdateClause,
    redact_args,
)
from src.commands.contracts.preview import preview_stub
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.playbooks.explanation import REDACTED, can_render, render_effect, render_node_explanation


def _node(args: dict, command: str = "ensure_task") -> dict:
    return {
        "action": {
            "command": command,
            "args": args,
            "on_success": "linked",
            "on_failure": "done",
            "output": {"as": "review"},
        }
    }


def test_explanation_uses_registered_contract_and_never_executes_it() -> None:
    explanation = render_node_explanation(
        "create",
        _node(
            {
                "project_id": "{{event.project_id}}",
                "dedup_key": "review:{{event.task_id}}",
                "title": "Review",
            }
        ),
        event_type="task.completed",
    )
    assert explanation is not None
    assert explanation.contract_fingerprint == CONTRACTS.fingerprint("ensure_task")
    assert explanation.effects[0].text == 'Create or reuse a task keyed by "dedup_key"'
    assert explanation.inputs[0].value.text == "this event's project"
    assert {item.target_node_id for item in explanation.outcomes} == {"linked", "done"}
    assert explanation.result and explanation.result.fields == ["task_id", "created"]


def test_unknown_fields_are_visible_and_uncontracted_nodes_remain_legacy() -> None:
    explanation = render_node_explanation(
        "create", _node({"dedup_key": "x", "title": "T", "future": 1})
    )
    assert explanation is not None and explanation.unrendered_fields == ["future"]
    assert render_node_explanation("unknown", {"action": {"command": "other", "args": {}}}) is None


def test_event_sensitive_value_is_redacted(monkeypatch) -> None:
    from src import event_schemas

    monkeypatch.setitem(
        event_schemas.EVENT_SCHEMAS,
        "secret.event",
        {
            "required": [],
            "optional": [],
            "fields": {"token": {"type": "string", "description": "token", "sensitive": True}},
        },
    )
    explanation = render_node_explanation(
        "create", _node({"dedup_key": "{{event.token}}", "title": "T"}), event_type="secret.event"
    )
    assert explanation is not None
    value = explanation.inputs[0].value
    assert value.text == REDACTED and value.redacted and value.raw is None


# -- Exhaustiveness over the closed effect-clause universe ------------------
#
# Iterating the *registered* effects only proves the ten built-ins render.  The
# roadmap's exit gate is stronger: "exhaustiveness tests fail when a new effect
# kind is introduced without a renderer", so the universe under test is
# ``EFFECT_CLAUSE_TYPES`` — every member of the discriminated union, whether or
# not any contract uses it yet.

_ONE_OF_EACH: dict[type, object] = {
    CreateClause: CreateClause(subject=EffectSubject.TASK),
    ReuseClause: ReuseClause(subject=EffectSubject.TASK, key_arg="name"),
    CreateOrReuseClause: CreateOrReuseClause(subject=EffectSubject.TASK, key_arg="name"),
    UpdateClause: UpdateClause(subject=EffectSubject.TASK),
    LinkClause: LinkClause(
        subject=EffectSubject.DEPENDENCY_EDGE, from_arg="name", to_arg="name"
    ),
    ResolveClause: ResolveClause(subject=EffectSubject.GATE, target_arg="name"),
}


def _sample(clause_type: type) -> object:
    return _ONE_OF_EACH.get(clause_type) or clause_type(subject=EffectSubject.TASK)


@pytest.mark.parametrize("clause_type", EFFECT_CLAUSE_TYPES, ids=lambda t: t.__name__)
def test_every_clause_kind_in_the_closed_universe_has_a_renderer(clause_type: type) -> None:
    clause = _sample(clause_type)
    assert can_render(clause)
    effect = render_effect(clause, {}, CommandPresentation(title="T", summary="s"))
    assert effect.operation == clause.kind
    assert effect.text and not effect.text.endswith(" ")


def test_the_sample_set_covers_the_whole_union() -> None:
    """A new clause type must arrive with a renderer *and* a sample here."""
    assert {type(_sample(t)) for t in EFFECT_CLAUSE_TYPES} == set(EFFECT_CLAUSE_TYPES)


# -- Synthetic contracts ----------------------------------------------------


class _Args(CommandArgs):
    token: str
    note: str | None = None


class _Value(CommandValue):
    identifier: str = ""


def _register(registry: ContractRegistry, **execution: object) -> None:
    values: dict[str, object] = {
        "name": "synthetic",
        "args_model": _Args,
        "result_model": _Value,
        "outcomes": (OutcomeSpec(name="done", classification=OutcomeClass.SUCCESS),),
        "capability": "synthetic",
        "side_effect": SideEffectClass.COMPOSITE,
        "idempotency": IdempotencySpec(mode="none"),
        "retry_safe": False,
    }
    values.update(execution)

    async def _invoke(_args, _ctx):  # pragma: no cover - never called
        raise AssertionError("explanations never execute")

    registry.register(
        CommandRegistration(
            "synthetic",
            CommandContract(
                execution=ExecutionContract(**values),
                presentation=CommandPresentation(title="Synthetic", summary="A synthetic command"),
            ),
            _invoke,
        )
    )


def test_a_contract_without_effect_clauses_gets_the_canonical_fallback() -> None:
    """Design spec §360/§389 — intent is never hidden and never invented."""
    registry = ContractRegistry()
    _register(registry)
    explanation = render_node_explanation(
        "n", _node({"token": "t"}, command="synthetic"), registry=registry
    )
    assert explanation is not None
    assert [e.model_dump() for e in explanation.effects] == [
        {
            "operation": "composite",
            "text": "Composite using token, note",
            "condition": None,
            "subject": None,
        }
    ]


def test_sensitive_argument_is_redacted_everywhere() -> None:
    registry = ContractRegistry()
    _register(registry, sensitive_args=frozenset({"token"}))
    contract = registry.require("synthetic").contract
    explanation = render_node_explanation(
        "n", _node({"token": "hunter2", "note": "safe"}, command="synthetic"), registry=registry
    )
    assert explanation is not None
    value = explanation.inputs[0].value
    assert value.text == REDACTED and value.redacted and value.raw is None
    assert "hunter2" not in explanation.model_dump_json()
    assert redact_args(contract, {"token": "hunter2"}) == {"token": REDACTED}
    assert "hunter2" not in preview_stub(contract, _Args(token="hunter2")).summary


def test_idempotency_and_retry_read_from_the_contract() -> None:
    registry = ContractRegistry()
    _register(
        registry,
        idempotency=IdempotencySpec(mode="keyed", key_field="token"),
        effects=(CreateOrReuseClause(subject=EffectSubject.GATE, key_arg="token"),),
        retry_safe=True,
    )
    explanation = render_node_explanation(
        "n", _node({"token": "t"}, command="synthetic"), registry=registry
    )
    assert explanation is not None
    assert explanation.idempotency == "Repeating with the same token reuses the existing gate"
    assert explanation.retry == "Safe to retry"
