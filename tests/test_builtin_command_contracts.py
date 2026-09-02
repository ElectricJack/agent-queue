"""The pipeline whitelist and typed registrations stay in lockstep."""

import pytest

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


def test_presentation_is_authored_and_names_only_real_fields() -> None:
    """Copy lives in one place, and every label points at something real.

    The renderer reads nothing but these labels, and the goldens in
    ``tests/fixtures/contracts/`` (which the dashboard fixtures import) are
    what it produces from them — so a stale key here is a string the operator
    would never see, silently.
    """
    from src.commands.contracts.builtin import PRESENTATIONS

    assert set(PRESENTATIONS) == CONTRACTS.names()
    for name in CONTRACTS.names():
        contract = CONTRACTS.require(name).contract
        execution, presentation = contract.execution, contract.presentation
        assert presentation is PRESENTATIONS[name]
        # Not the auto-generated shape this file used to emit.
        assert presentation.title != name.replace("_", " ").title()
        assert presentation.summary.endswith(".")
        assert set(presentation.arg_labels) <= set(execution.args_model.model_fields)
        assert set(presentation.result_labels) <= set(execution.result_model.model_fields)
        assert set(presentation.outcome_labels) == {o.name for o in execution.outcomes}
        assert set(presentation.subject_labels) == {c.subject.value for c in execution.effects}


def test_golden_fingerprints() -> None:
    """Pinned execution fingerprints for the ten built-ins.

    Regenerating ``tests/fixtures/contracts/fingerprints.json`` stales every
    Package-2 artifact compiled against the old registry fingerprint.  Confirm
    the execution-contract change was intended before regenerating.  A
    presentation-only edit must never move these.
    """
    import json
    from pathlib import Path

    golden = json.loads(
        (Path(__file__).parent / "fixtures" / "contracts" / "fingerprints.json").read_text()
    )
    assert {name: CONTRACTS.fingerprint(name) for name in sorted(CONTRACTS.names())} == golden


async def test_production_wiring_makes_the_registered_invokes_operational() -> None:
    """``Orchestrator.set_command_handler`` installs the adapter's handler.

    Registering an ``invoke`` that raises "no CommandHandler provider
    installed" in every process is a registration, not a command; this is the
    seam that makes it real.
    """
    from types import SimpleNamespace

    from src.commands.contracts import builtin
    from src.commands.contracts.builtin import EnsureTaskArgs
    from src.orchestrator.core import Orchestrator

    calls: list[tuple[str, dict]] = []

    class _Handler:
        _active_project_id = None

        async def execute(self, name: str, args: dict) -> dict:
            calls.append((name, args))
            return {"task_id": "t-1", "created": True}

    previous = builtin._handler_provider
    builtin.set_handler_provider(None)
    try:
        # Unbound call: the seam is what is under test, not orchestrator setup.
        Orchestrator.set_command_handler(SimpleNamespace(_command_handler=None), _Handler())
        assert builtin.handler_provider_installed()
        result = await CONTRACTS.require("ensure_task").invoke(
            EnsureTaskArgs(dedup_key="d", title="T"), None
        )
        assert calls == [("ensure_task", {"dedup_key": "d", "title": "T"})]
        assert result.outcome == "created" and result.value.task_id == "t-1"
    finally:
        builtin.set_handler_provider(previous)


async def test_an_uninstalled_provider_fails_loudly() -> None:
    from src.commands.contracts import builtin
    from src.commands.contracts.builtin import EnsureTaskArgs

    previous = builtin._handler_provider
    builtin.set_handler_provider(None)
    try:
        with pytest.raises(RuntimeError, match="no CommandHandler provider installed"):
            await CONTRACTS.require("ensure_task").invoke(
                EnsureTaskArgs(dedup_key="d", title="T"), None
            )
    finally:
        builtin.set_handler_provider(previous)
