"""Pure contract-shape tests for the K03 knowledge/record command set.

No database, no dispatch: these pin the *typed surface* that every adapter —
CLI verbs, MCP tools, REST routes, playbook steps — is generated from.  Two
properties are the load-bearing ones for K03: (1) every one of the fourteen
worker-safe commands is a registered contract whose ``capability`` is its own
name (so the command gate and the service gate share one authority), and
(2) the argument models are closed and validated (``extra="forbid"``), so a
typo from any surface is rejected before it reaches a handler rather than
silently dropped.
"""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from src.commands.contracts.knowledge import (
    CATEGORY as KNOWLEDGE_CATEGORIES,
    KnowledgeCreateArgs,
    KnowledgeDiffArgs,
    KnowledgeHistoryArgs,
    KnowledgeListArgs,
    KnowledgeRestoreArgs,
    KnowledgeRetireArgs,
    KnowledgeShowArgs,
    KnowledgeUpdateArgs,
)
from src.commands.contracts.models import SideEffectClass
from src.commands.contracts.records import (
    LinkCreateArgs,
    LinkListArgs,
    LinkRemoveArgs,
    RecordCapabilitiesArgs,
    RecordSearchArgs,
    RecordShowArgs,
)
from src.commands.contracts.registry import CONTRACTS

# The fourteen worker-safe K03 commands (plan §7).  retire/restore are
# supervisor-only at runtime but still registered contracts here.
ALL_COMMANDS = frozenset(
    (
        "knowledge_create",
        "knowledge_list",
        "knowledge_show",
        "knowledge_update",
        "knowledge_history",
        "knowledge_diff",
        "knowledge_retire",
        "knowledge_restore",
        "record_show",
        "record_search",
        "record_capabilities",
        "link_create",
        "link_list",
        "link_remove",
    )
)

# Commands whose idempotency is keyed on ``idempotency_key`` (the mutators);
# every other command is natural-idempotent (a repeat reads the same answer).
KEYED = frozenset(
    (
        "knowledge_create",
        "knowledge_update",
        "knowledge_retire",
        "knowledge_restore",
        "link_create",
        "link_remove",
    )
)

# side_effect class expected per command — this is the dispatch contract the
# CLI / MCP / REST verbs render, and a drift here is user-visible.
SIDE_EFFECTS = {
    "knowledge_create": SideEffectClass.CREATE,
    "knowledge_list": SideEffectClass.READ,
    "knowledge_show": SideEffectClass.READ,
    "knowledge_update": SideEffectClass.UPDATE,
    "knowledge_history": SideEffectClass.READ,
    "knowledge_diff": SideEffectClass.READ,
    "knowledge_retire": SideEffectClass.RESOLVE,
    "knowledge_restore": SideEffectClass.RESOLVE,
    "record_show": SideEffectClass.READ,
    "record_search": SideEffectClass.READ,
    "record_capabilities": SideEffectClass.READ,
    "link_create": SideEffectClass.LINK,
    "link_list": SideEffectClass.READ,
    "link_remove": SideEffectClass.LINK,
}

# (command, args model) pairs, so the argument-validation cases below can be
# written once per model regardless of which contract it serves.
MODELS = {
    "knowledge_create": KnowledgeCreateArgs,
    "knowledge_list": KnowledgeListArgs,
    "knowledge_show": KnowledgeShowArgs,
    "knowledge_update": KnowledgeUpdateArgs,
    "knowledge_history": KnowledgeHistoryArgs,
    "knowledge_diff": KnowledgeDiffArgs,
    "knowledge_retire": KnowledgeRetireArgs,
    "knowledge_restore": KnowledgeRestoreArgs,
    "record_show": RecordShowArgs,
    "record_search": RecordSearchArgs,
    "record_capabilities": RecordCapabilitiesArgs,
    "link_create": LinkCreateArgs,
    "link_list": LinkListArgs,
    "link_remove": LinkRemoveArgs,
}


def test_all_fourteen_k03_commands_are_registered():
    missing = ALL_COMMANDS - CONTRACTS.names()
    assert not missing, f"unregistered K03 contracts: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(ALL_COMMANDS))
def test_capability_is_the_command_name(name):
    registration = CONTRACTS.get(name)
    assert registration is not None
    assert registration.contract.execution.capability == name


@pytest.mark.parametrize("name", sorted(ALL_COMMANDS))
def test_side_effect_matches_the_dispatch_contract(name):
    registration = CONTRACTS.get(name)
    assert registration.contract.execution.side_effect is SIDE_EFFECTS[name]


@pytest.mark.parametrize("name", sorted(ALL_COMMANDS))
def test_idempotency_mode_matches_the_mutation_class(name):
    spec = CONTRACTS.get(name).contract.execution.idempotency
    if name in KEYED:
        assert spec.mode == "keyed"
        assert spec.key_field == "idempotency_key"
        assert "idempotency_key" in MODELS[name].model_fields
    else:
        assert spec.mode == "natural"
        # The keyless reads and the read-shaped records never take a key.
        assert "idempotency_key" not in MODELS[name].model_fields


@pytest.mark.parametrize("name,model", sorted(MODELS.items()))
def test_args_models_are_closed(name, model):
    # Unknown keys are rejected, not dropped: a CLI/MCP typo must be an error.
    with pytest.raises(ValidationError):
        model(**{k: v for k, v in _valid_for(name).items()}, bogus_key="nope")


def _valid_for(name):
    base = {"project_id": "p"}
    values = {
        "knowledge_create": {
            "title": "t",
            "body": "b",
            "category": "fact",
            "idempotency_key": "k1",
        },
        "knowledge_list": {},
        "knowledge_show": {"identity": "record:x"},
        "knowledge_update": {"identity": "record:x", "idempotency_key": "k1"},
        "knowledge_history": {"identity": "record:x"},
        "knowledge_diff": {"identity": "record:x", "from_revision": "a", "to_revision": "b"},
        "knowledge_retire": {"identity": "record:x", "reason": "r", "idempotency_key": "k1"},
        "knowledge_restore": {
            "identity": "record:x",
            "revision_id": "rev",
            "reason": "r",
            "idempotency_key": "k1",
        },
        "record_show": {"identity": "record:x"},
        "record_search": {},
        "record_capabilities": {},
        "link_create": {"identity": "record:x", "operations": [{"action": "add", "target": "record:y"}], "idempotency_key": "k1"},
        "link_list": {"identity": "record:x"},
        "link_remove": {"identity": "record:x", "link_id": "lid", "idempotency_key": "k1"},
    }
    if name == "record_capabilities":
        return {}  # project_id is optional here
    return {**base, **values[name]}


@pytest.mark.parametrize(
    ("missing", "error"),
    [
        (("project_id",), "missing required field"),
    ],
)
def test_knowledge_create_requires_the_core_fields(missing, error):
    with pytest.raises(ValidationError):
        KnowledgeCreateArgs(
            **{k: v for k, v in {"project_id": "p", "title": "t", "body": "b", "category": "fact",
                                 "idempotency_key": "k"}.items() if k not in missing}
        )


def test_category_is_a_seven_value_closed_enum():
    expected = {"fact", "decision", "policy", "procedure", "incident", "reference", "note"}
    assert set(typing.get_args(KNOWLEDGE_CATEGORIES)) == expected
    with pytest.raises(ValidationError):
        KnowledgeCreateArgs(
            project_id="p", title="t", body="b", category="memory", idempotency_key="k"
        )


def test_idempotency_key_bounds():
    with pytest.raises(ValidationError):
        KnowledgeCreateArgs(project_id="p", title="t", body="b", category="fact", idempotency_key="")
    with pytest.raises(ValidationError):
        KnowledgeCreateArgs(
            project_id="p", title="t", body="b", category="fact", idempotency_key="x" * 129
        )
    ok = KnowledgeCreateArgs(
        project_id="p", title="t", body="b", category="fact", idempotency_key="k"
    )
    assert ok.idempotency_key == "k"


def test_search_limit_bound():
    with pytest.raises(ValidationError):
        RecordSearchArgs(project_id="p", limit=0)
    with pytest.raises(ValidationError):
        RecordSearchArgs(project_id="p", limit=101)
    assert RecordSearchArgs(project_id="p", limit=1).limit == 1


def test_link_create_requires_at_least_one_operation():
    with pytest.raises(ValidationError):
        LinkCreateArgs(
            project_id="p", identity="record:x", operations=[], idempotency_key="k"
        )
    ok = LinkCreateArgs(
        project_id="p",
        identity="record:x",
        operations=[{"action": "add", "target": "record:y", "link_type": "references"}],
        idempotency_key="k",
    )
    assert ok.operations[0]["action"] == "add"
