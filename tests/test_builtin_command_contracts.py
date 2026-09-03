"""The pipeline whitelist and typed registrations stay in lockstep."""

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import CreateTaskArgs, EnsureTaskArgs, _outcome_of
from src.playbooks.pipeline_compiler import PIPELINE_COMMAND_WHITELIST


def test_all_pipeline_commands_have_one_contract_registration() -> None:
    assert CONTRACTS.names() == PIPELINE_COMMAND_WHITELIST
    assert len(CONTRACTS.names()) == 11


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
    assert _outcome_of("stop_task", {"stopped": "t-1"}) == "stopped"
    assert (
        _outcome_of("stop_task", {"error": "Task is not in progress (status: COMPLETED)"})
        == "not_running"
    )
    assert _outcome_of("stop_task", {"error": "Task 't-1' not found"}) == "rejected"


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
    """Pinned execution fingerprints for the built-ins.

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


async def test_registered_invoke_reenters_handler_with_its_supplied_principal() -> None:
    """A narrowed executor principal must replace a broader ambient caller."""
    from src.commands.contracts import builtin
    from src.commands.principal import (
        ExecutionPrincipal,
        PrincipalKind,
        current_principal,
        principal_context,
    )
    from src.profiles.capabilities import CapabilityPolicy, DENY_ALL

    broad = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        session_id="outer",
        policy=CapabilityPolicy.from_namespaces(aq_commands=["create_task"]),
    )
    narrowed = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        session_id="child",
        policy=DENY_ALL,
    )
    seen = []

    class _Handler:
        async def execute(self, _name: str, args: dict) -> dict:
            seen.append(current_principal())
            return {
                "created": "child-1",
                "task_id": "child-1",
                "status": "DEFINED",
                "title": args["title"],
                "project_id": "p",
            }

    previous = builtin._handler_provider
    builtin.set_handler_provider(lambda: _Handler())
    try:
        with principal_context(broad):
            await CONTRACTS.require("create_task").invoke(
                CreateTaskArgs(title="child"), narrowed
            )
    finally:
        builtin.set_handler_provider(previous)

    assert seen == [narrowed]


async def test_stop_task_is_dispatchable_and_an_already_stopped_child_is_not_a_violation() -> None:
    """``stop_task`` is what a parent run dispatches to cancel its child.

    Both branches matter to the cancellation path: a running child stops, and
    a child that already left ``IN_PROGRESS`` yields the ``not_running``
    success outcome with an empty value — never ``contract_violation``, which
    is what a required ``stopped`` field would otherwise produce.
    """
    from src.commands.contracts import builtin
    from src.commands.contracts.builtin import StopTaskArgs

    replies: list[dict] = []
    calls: list[tuple[str, dict]] = []

    class _Handler:
        _active_project_id = None

        async def execute(self, name: str, args: dict) -> dict:
            calls.append((name, args))
            return replies.pop(0)

    previous = builtin._handler_provider
    builtin.set_handler_provider(lambda: _Handler())
    try:
        invoke = CONTRACTS.require("stop_task").invoke
        replies.append({"stopped": "child-1"})
        result = await invoke(StopTaskArgs(task_id="child-1"), None)
        assert (result.outcome, result.value.stopped) == ("stopped", "child-1")

        replies.append({"error": "Task is not in progress (status: COMPLETED)"})
        result = await invoke(StopTaskArgs(task_id="child-1"), None)
        assert result.outcome == "not_running"

        replies.append({"error": "Task 'child-9' not found"})
        result = await invoke(StopTaskArgs(task_id="child-9"), None)
        assert result.outcome == "rejected"
        assert calls == [
            ("stop_task", {"task_id": "child-1"}),
            ("stop_task", {"task_id": "child-1"}),
            ("stop_task", {"task_id": "child-9"}),
        ]
    finally:
        builtin.set_handler_provider(previous)


def test_stop_task_declares_a_cancellation_effect_the_renderer_can_explain() -> None:
    from src.playbooks.explanation import render_effect

    contract = CONTRACTS.require("stop_task").contract
    execution = contract.execution
    assert execution.side_effect.value == "update"
    assert execution.retry_safe and execution.idempotency.mode == "natural"
    assert {o.name: o.classification.value for o in execution.outcomes} == {
        "stopped": "success",
        "not_running": "success",
        "rejected": "failure",
    }
    (clause,) = execution.effects
    assert render_effect(clause, {"task_id": "t-1"}, contract.presentation).text == (
        "Update the task's execution"
    )


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
