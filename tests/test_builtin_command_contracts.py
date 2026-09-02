"""The pipeline whitelist and typed registrations stay in lockstep."""

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import CreateTaskArgs, EnsureTaskArgs, _outcome_of
from src.playbooks.pipeline_compiler import PIPELINE_COMMAND_WHITELIST


def test_all_pipeline_commands_have_one_contract_registration() -> None:
    assert CONTRACTS.names() == PIPELINE_COMMAND_WHITELIST
    assert len(CONTRACTS.names()) == 10


def test_builtin_contract_capabilities_are_the_command_names() -> None:
    for name in CONTRACTS.names():
        assert CONTRACTS.required_capability(name) == name


def test_builtin_argument_models_are_closed_but_preserve_handler_options() -> None:
    assert (
        EnsureTaskArgs(dedup_key="d", title="T", initial_status="PAUSED").initial_status == "PAUSED"
    )
    assert CreateTaskArgs(title="T", requires_kinds=["project-repo"]).requires_kinds == [
        "project-repo"
    ]


def test_legacy_shapes_map_to_declared_outcomes_without_success_key_heuristics() -> None:
    assert _outcome_of("add_dependency", {"ok": True}) == "linked"
    assert _outcome_of("ensure_task", {"created": False}) == "reused"
    assert (
        _outcome_of("gate_resolve", {"error": "routing gates can only be resolved"})
        == "refused_routing_gate"
    )
