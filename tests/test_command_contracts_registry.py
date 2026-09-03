"""Contract-boundary invariants for Playbook V2 commands."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.commands.contracts.models import (
    CreateClause,
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandValue,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.contracts.registry import (
    CommandRegistration,
    ContractRegistrationError,
    ContractRegistry,
)


class Args(CommandArgs):
    name: str


class Value(CommandValue):
    identifier: str


def _contract(**changes: object) -> CommandContract[Args, Value]:
    values: dict[str, object] = {
        "name": "example",
        "args_model": Args,
        "result_model": Value,
        "outcomes": (OutcomeSpec(name="done", classification=OutcomeClass.SUCCESS),),
        "capability": "example",
        "side_effect": SideEffectClass.READ,
        "idempotency": IdempotencySpec(mode="natural"),
        "retry_safe": True,
    }
    values.update(changes)
    execution = ExecutionContract(**values)
    return CommandContract(
        execution=execution,
        presentation=CommandPresentation(title="Example", summary="Read an example"),
    )


async def _invoke(_args: Args, _ctx: object):
    raise AssertionError("not invoked")


def test_register_rejects_duplicate_name() -> None:
    registry = ContractRegistry()
    registration = CommandRegistration("example", _contract(), _invoke)
    registry.register(registration)
    with pytest.raises(ContractRegistrationError, match="already registered"):
        registry.register(registration)


def test_execution_validation_rejects_reserved_and_wildcard_names() -> None:
    with pytest.raises(ValidationError):
        _contract(
            outcomes=(OutcomeSpec(name="contract_violation", classification=OutcomeClass.FAILURE),)
        )
    with pytest.raises(ValidationError):
        _contract(capability="task_*")


def test_fingerprint_excludes_presentation_but_covers_execution() -> None:
    first = _contract()
    copy_changed = CommandContract(
        execution=first.execution,
        presentation=CommandPresentation(title="Different", summary="Different copy"),
    )
    execution_changed = _contract(retry_safe=False)
    assert first.fingerprint() == copy_changed.fingerprint()
    assert first.fingerprint() != execution_changed.fingerprint()


def test_execution_validation_enforces_model_references_and_idempotency_shape() -> None:
    with pytest.raises(ValidationError, match="key_field"):
        _contract(idempotency=IdempotencySpec(mode="keyed", key_field="missing"))
    with pytest.raises(ValidationError, match="effect clause"):
        _contract(effects=(CreateClause(subject="task", when={"arg_present": "missing"}),))


def test_registry_fingerprint_is_independent_of_registration_order() -> None:
    first, second = ContractRegistry(), ContractRegistry()
    first.register(CommandRegistration("example", _contract(), _invoke))
    second.register(CommandRegistration("example", _contract(), _invoke))
    assert first.registry_fingerprint() == second.registry_fingerprint()


def test_a_bare_registry_registers_nothing_and_the_singleton_autoloads() -> None:
    """Built-ins arrive on first read of the singleton, not at import time."""
    from src.commands.contracts import CONTRACTS

    assert ContractRegistry().names() == frozenset()
    assert len(CONTRACTS.names()) == 11
    # Idempotent: a second read does not re-register and raise "already registered".
    assert CONTRACTS.names() == CONTRACTS.names()


def test_the_explanation_module_can_be_imported_first() -> None:
    """``import src.playbooks.explanation`` must not hit a circular import.

    ``ContractRegistry.register`` imports ``can_render`` from that module, so
    registering the built-ins as an import-time side effect of
    ``src.commands.contracts`` re-entered a half-initialised ``explanation``
    and raised ``ImportError``.  A subprocess is the only honest check: the
    modules are already in ``sys.modules`` inside the test session.
    """
    import subprocess
    import sys

    for first in ("src.playbooks.explanation", "src.playbooks.graph_view", "src.commands.contracts"):
        proc = subprocess.run(
            [sys.executable, "-c", f"import {first}; from src.commands.contracts import CONTRACTS;"
             " assert len(CONTRACTS.names()) == 11"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"importing {first} first failed:\n{proc.stderr}"


def test_effect_clause_types_matches_the_union() -> None:
    """The explicit tuple cannot drift from the discriminated union."""
    import typing

    from src.commands.contracts.models import EFFECT_CLAUSE_TYPES, EffectClause

    union = typing.get_args(typing.get_args(EffectClause)[0])
    assert set(EFFECT_CLAUSE_TYPES) == set(union)


def test_register_refuses_a_clause_the_renderer_cannot_render(monkeypatch) -> None:
    """Roadmap: "Fail contract registration when an effect cannot be rendered"."""
    from src.playbooks import explanation

    monkeypatch.setattr(explanation, "can_render", lambda _clause: False)
    with pytest.raises(ContractRegistrationError, match="no renderer"):
        ContractRegistry().register(
            CommandRegistration(
                "example", _contract(effects=(CreateClause(subject="task"),)), _invoke
            )
        )
