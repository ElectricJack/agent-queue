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
from src.docs_urls import DEFAULT_DOCS_BASE_URL, command_docs_url


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


def _builtin_names() -> frozenset[str]:
    """Every contract ``register_builtin_contracts`` installs on a fresh registry.

    The singleton must carry exactly these (no more: nothing else registers at
    import time; no fewer: the autoload ran) — derived rather than a literal
    count so adding a built-in contract does not silently rot this ratchet.
    """
    from src.commands.contracts import register_builtin_contracts

    registry = ContractRegistry()
    register_builtin_contracts(registry)
    assert registry.names(), "register_builtin_contracts installed nothing"
    return registry.names()


def test_a_bare_registry_registers_nothing_and_the_singleton_autoloads() -> None:
    """Built-ins arrive on first read of the singleton, not at import time."""
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import PRESENTATIONS

    assert ContractRegistry().names() == frozenset()
    assert CONTRACTS.names() == _builtin_names()
    assert set(PRESENTATIONS) <= CONTRACTS.names()
    assert {"create_task", "ensure_task", "message_send", "stop_task"} <= CONTRACTS.names()
    # Idempotent: a second read does not re-register and raise "already registered".
    assert CONTRACTS.names() == CONTRACTS.names()


def test_every_builtin_contract_has_the_convention_derived_help_url() -> None:
    """New commands gain documentation links at registry registration time."""
    from src.commands.contracts import register_builtin_contracts

    registry = ContractRegistry()
    register_builtin_contracts(registry)

    assert registry.names()
    for name in registry.names():
        assert registry.require(name).contract.presentation.help_url == command_docs_url(
            DEFAULT_DOCS_BASE_URL, name
        )


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

    expected = len(_builtin_names())
    for first in ("src.playbooks.explanation", "src.commands.contracts"):
        proc = subprocess.run(
            [sys.executable, "-c", f"import {first}; from src.commands.contracts import CONTRACTS;"
             f" assert len(CONTRACTS.names()) == {expected}, len(CONTRACTS.names());"
             " assert 'message_send' in CONTRACTS.names() and 'create_task' in CONTRACTS.names()"],
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


async def test_the_builtin_adapter_sends_declared_defaults_and_explicit_nulls() -> None:
    """A declared default reaches the handler, and an explicit null stays distinct from omission.

    ``MessageSendArgs.from_kind`` defaults to ``system``; when the adapter
    dropped defaults, ``message_send`` fell back to ``user`` for every
    playbook notice that relied on it.
    """
    from unittest.mock import AsyncMock

    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import (
        EnsureTaskArgs,
        MessageSendArgs,
        set_handler_provider,
    )

    handler = AsyncMock()
    set_handler_provider(lambda: handler)
    try:
        handler.execute.return_value = {"message_id": "msg-1", "state": "queued"}
        args = MessageSendArgs(
            to_kind="session", to_id="supervisor-proj", body="hi", from_id="playbook:x"
        )
        result = await CONTRACTS.require("message_send").invoke(args, None)
        assert result.outcome == "queued"
        assert result.value.message_id == "msg-1"
        assert handler.execute.await_args.args == (
            "message_send",
            {
                "to_kind": "session",
                "to_id": "supervisor-proj",
                "body": "hi",
                "from_id": "playbook:x",
                "from_kind": "system",
            },
        )

        handler.execute.return_value = {"task_id": "t-1", "created": True}
        ensure = CONTRACTS.require("ensure_task")
        await ensure.invoke(EnsureTaskArgs(dedup_key="k", title="t"), None)
        assert handler.execute.await_args.args[1] == {"dedup_key": "k", "title": "t"}
        await ensure.invoke(EnsureTaskArgs(dedup_key="k", title="t", parent_id=None), None)
        assert handler.execute.await_args.args[1] == {
            "dedup_key": "k", "title": "t", "parent_id": None,
        }
    finally:
        set_handler_provider(None)


def test_presentation_labels_name_real_fields() -> None:
    """Every label key must name something the execution contract actually has.

    ``PRESENTATIONS`` is hand-authored beside models it cannot see, so a
    renamed field leaves copy behind that labels nothing and a reviewer
    reading the labels believes a field exists.  ``list_tasks`` is why this
    test is here: its ``result_labels`` advertised ``by_project``, a key no
    ``list_tasks`` path has ever returned, alongside a result model that
    *required* it — and the label was the only visible trace of the mismatch.
    """
    from src.commands.contracts import CONTRACTS

    dead: list[str] = []
    for name in sorted(CONTRACTS.names()):
        contract = CONTRACTS.require(name).contract
        execution, presentation = contract.execution, contract.presentation
        for key in presentation.arg_labels:
            if key not in execution.args_model.model_fields:
                dead.append(f"{name}: arg_labels[{key!r}] is not an argument")
        for key in presentation.result_labels:
            if key not in execution.result_model.model_fields:
                dead.append(f"{name}: result_labels[{key!r}] is not a result field")
        outcomes = {outcome.name for outcome in execution.outcomes}
        for key in presentation.outcome_labels:
            if key not in outcomes:
                dead.append(f"{name}: outcome_labels[{key!r}] is not a declared outcome")
        subjects = {clause.subject.value for clause in execution.effects}
        for key in presentation.subject_labels:
            if key not in subjects:
                dead.append(f"{name}: subject_labels[{key!r}] is not an effect subject")
    assert dead == []


# --------------------------------------------------------------------------
# list_tasks — the adapter against the real handler, in every display mode
# --------------------------------------------------------------------------


@pytest.fixture
async def list_tasks_handler(tmp_path):
    """A real ``CommandHandler`` on a real database, seeded with a task tree.

    The defect this guards is a disagreement between two files, so nothing
    here may stub either of them: the contract adapter has to call the real
    ``_cmd_list_tasks`` and validate the payload it really returns.
    """
    from unittest.mock import MagicMock

    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.database import Database
    from src.models import Project, Task, TaskStatus
    from src.orchestrator import Orchestrator
    from tests.db_fixtures import lease_dsn

    dsn = lease_dsn("test.db")
    database = Database(dsn)
    await database.initialize()
    await database.create_project(Project(id="proj", name="Test Project"))
    await database.create_task(
        Task(id="root", project_id="proj", title="root", description="root",
             status=TaskStatus.READY)
    )
    await database.create_task(
        Task(id="child", project_id="proj", title="child", description="child",
             status=TaskStatus.READY, parent_task_id="root")
    )
    config = AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=dsn),
        data_dir=str(tmp_path / "data"),
    )
    orchestrator = Orchestrator(config)
    orchestrator.db = database
    orchestrator.git = MagicMock()
    try:
        yield CommandHandler(orchestrator, config)
    finally:
        await database.close()


@pytest.mark.parametrize("display_mode", ["flat", "tree", "compact"])
async def test_the_list_tasks_adapter_accepts_every_display_mode(
    list_tasks_handler, display_mode
) -> None:
    """A playbook step calling ``list_tasks`` must not get ``contract_violation``.

    ``ListTasksValue`` used to require ``by_project`` and ``project_count`` —
    keys that belong to ``list_active_tasks_all_projects`` and that neither
    ``_list_tasks_flat`` nor ``_list_tasks_hierarchical`` returns.  ``_adapter``
    copies only the fields the value model declares and then constructs it, so
    the two missing required fields raised ``ValidationError`` and *every*
    call, in *every* display mode, came back ``contract_violation``.  The
    compiler validated against the same lying model, so an author got a clean
    compile and a run-time failure.
    """
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import ListTasksArgs, set_handler_provider

    set_handler_provider(lambda: list_tasks_handler)
    try:
        result = await CONTRACTS.require("list_tasks").invoke(
            ListTasksArgs(project_id="proj", display_mode=display_mode), None
        )
    finally:
        set_handler_provider(None)

    assert result.outcome == "listed", result.summary
    value = result.value
    assert value.display_mode == display_mode
    if display_mode == "flat":
        assert sorted(task["id"] for task in value.tasks) == ["child", "root"]
        assert value.total == 2
        assert value.trees == []
    else:
        assert [entry["root"]["id"] for entry in value.trees] == ["root"]
        assert value.total_root_tasks == 1
        assert value.total_tasks == 2
        assert value.tasks == []


async def test_list_tasks_models_every_key_its_own_arguments_can_produce(
    list_tasks_handler,
) -> None:
    """The value model is checked against the handler, not against itself.

    A default on a missing field keeps the step green, so the model can drift
    back into fiction without any call failing.  This walks the payload the
    real handler returns for each mode the contract can ask for and fails on
    a key the model does not declare, which is what would have to change for
    the contract to start lying again.
    """
    from src.commands.contracts.builtin import ListTasksValue

    # Not part of the result: ``success`` is the envelope every handler adds,
    # and ``label_filter_scope`` needs a ``labels`` filter ``ListTasksArgs``
    # deliberately does not expose.
    envelope = {"success", "label_filter_scope"}
    declared = set(ListTasksValue.model_fields)

    for args in (
        {"project_id": "proj"},
        {"project_id": "proj", "show_dependencies": True},
        {"project_id": "proj", "display_mode": "tree"},
        {"project_id": "proj", "display_mode": "tree", "show_dependencies": True},
        {"project_id": "proj", "display_mode": "compact"},
    ):
        raw = await list_tasks_handler.execute("list_tasks", args)
        unmodelled = set(raw) - declared - envelope
        assert unmodelled == set(), f"{args} returned unmodelled keys {sorted(unmodelled)}"
        # And the model accepts the payload exactly as ``_adapter`` builds it.
        ListTasksValue(**{field: raw[field] for field in declared if field in raw})
